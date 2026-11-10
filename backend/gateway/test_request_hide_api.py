"""가상 계정·PDF로 본인 이력 숨김과 감사 데이터 보존을 검증한다."""

import json
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.db import DatabaseError
from django.db.models.query import QuerySet
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import NotAuthenticated

from accounts.roles import ADMIN_GROUP_NAME, USER_GROUP_NAME

from .approval_lifecycle import expire_due_approvals
from .models import DetectionSpan, Document, MaskingMapping, Request, SensitiveTerm
from .request_visibility import hide_request
from .storage import document_storage
from .test_intake_api import pdf_bytes


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RequestHideAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.member = cls.account("USR72001", cls.user_group)
        cls.other = cls.account("USR72002", cls.user_group)
        cls.admin = cls.account("ADM72001", cls.admin_group)
        cls.operator = cls.account("OPS72001", None, is_staff=True, is_superuser=True)

    @classmethod
    def account(cls, username, group, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, full_name="숨김 검증용 가상 사용자", password="SecurePass1!", **kwargs,
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.member)

    def make_request(self, **kwargs):
        values = {"user": self.member, "client_request_key": uuid4(), "instruction": "가상 의뢰"}
        values.update(kwargs)
        return Request.objects.create(**values)

    def url(self, record, name="request-hide"):
        return reverse(f"gateway:{name}", kwargs={"request_id": record.pk})

    def headers(self):
        return {"HTTP_X_CSRFTOKEN": self.client.get(reverse("accounts:csrf")).json()["csrf_token"]}

    def hide(self, record):
        return self.client.post(self.url(record), "{}", content_type="application/json", **self.headers())

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertIn("no-store", response["Cache-Control"])

    def test_owner_hides_history_with_empty_response_and_keeps_request(self):
        record = self.make_request(state="completed", answer_text="가상 완료 답변", finished_at=timezone.now())
        response = self.hide(record)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertIn("no-store", response["Cache-Control"])
        record.refresh_from_db()
        self.assertIsNotNone(record.hidden_at)
        self.assertEqual(record.updated_at, record.hidden_at)
        self.assertEqual(record.state, "completed")
        self.assertEqual(record.answer_text, "가상 완료 답변")
        self.assertEqual(Request.objects.count(), 1)

    def test_repeat_hide_keeps_first_timestamp_and_does_not_write_again(self):
        record = self.make_request()
        self.assertEqual(self.hide(record).status_code, 204)
        before = Request.objects.values().get(pk=record.pk)
        with patch("gateway.request_visibility.timezone.now", return_value=before["hidden_at"] + timedelta(minutes=1)):
            self.assertEqual(self.hide(record).status_code, 204)
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_hiding_any_state_preserves_all_processing_fields(self):
        for state in Request.State.values:
            with self.subTest(state=state):
                record = self.make_request(
                    state=state, input_text="가상 검사 원문", warning_codes=["FAKE_WARNING"],
                    inspection_versions={"test": "fictional"},
                    answer_text="가상 완료 답변" if state == "completed" else None,
                )
                before = Request.objects.values().get(pk=record.pk)
                self.assertEqual(self.hide(record).status_code, 204)
                after = Request.objects.values().get(pk=record.pk)
                for field in ("hidden_at", "updated_at"):
                    before.pop(field)
                    after.pop(field)
                self.assertEqual(after, before)

    def test_hidden_request_disappears_from_owner_list_and_detail(self):
        hidden = self.make_request(state="completed", answer_text="가상 완료 답변")
        visible = self.make_request()
        self.assertEqual(self.hide(hidden).status_code, 204)
        response = self.client.get(reverse("gateway:request-intake"), {"page_size": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["total_pages"], 1)
        self.assertEqual([row["request_id"] for row in response.json()["results"]], [str(visible.pk)])
        self.assert_error(self.client.get(self.url(hidden, "request-intake-detail")), 404, "REQUEST_NOT_FOUND")
        self.assert_error(self.client.get(reverse("gateway:request-intake"), {"hidden": "true"}), 400, "INVALID_INPUT")

    def test_pdf_file_remains_private_and_admin_can_still_open_it(self):
        record = self.make_request()
        content = pdf_bytes()
        with TemporaryDirectory() as root, override_settings(PRIVATE_DOCUMENT_ROOT=root):
            storage = document_storage()
            key = storage.save("requests/fictional/hide-test.pdf", ContentFile(content))
            Document.objects.create(
                request=record, original_filename="가상.pdf", storage_object_key=key,
                size_bytes=len(content), page_count=1, extracted_text="가상 본문", extraction_status="ready",
            )
            before = Document.objects.values().get(pk=record.pk)
            self.assertEqual(self.hide(record).status_code, 204)
            self.assertTrue(storage.exists(key))
            self.assertEqual(Document.objects.values().get(pk=record.pk), before)
            self.assert_error(self.client.get(self.url(record, "request-file")), 404, "DOCUMENT_NOT_FOUND")
            self.client.force_login(self.other)
            self.assert_error(self.client.get(self.url(record, "request-file")), 404, "DOCUMENT_NOT_FOUND")
            self.client.force_login(self.admin)
            response = self.client.get(self.url(record, "request-file"), HTTP_ACCEPT="application/pdf")
            self.assertEqual(response.status_code, 200)
            # 테스트 클라이언트의 스트림 소비가 응답을 닫는다. close()를 다시 호출하지 않는다.
            self.assertEqual(b"".join(response.streaming_content), content)

    def test_admin_audit_and_statistics_keep_hidden_history_and_mapping(self):
        record = self.make_request(
            input_text="가상기업", state="completed", answer_text="비공개 가상 답변",
            inspection_status="complete", processing_route="external",
            approval_status="approved", decided_at=timezone.now(), finished_at=timezone.now(),
            delivery_status="confirmed_sent", external_send_started_at=timezone.now(), warning_codes=["FAKE_WARNING"],
        )
        term = SensitiveTerm.objects.create(value="가상기업", label="company")
        mapping = MaskingMapping.objects.create(request=record, sensitive_term=term, placeholder="FAKE_TOKEN")
        span = DetectionSpan.objects.create(request=record, sensitive_term=term, mapping=mapping, input_source="instruction", start_offset=0, end_offset=4)
        day = record.created_at.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
        self.client.force_login(self.admin)
        list_url = reverse("gateway_admin:request-list")
        stats_url = reverse("gateway_admin:statistics")
        filters = {"date_from": day, "date_to": day, "request_id": str(record.pk)}
        before_list = self.client.get(list_url, {"request_id": str(record.pk)}).json()
        before_stats = self.client.get(stats_url, filters).json()
        self.client.force_login(self.member)
        self.assertEqual(self.hide(record).status_code, 204)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(list_url, {"request_id": str(record.pk)}).json(), before_list)
        after_stats = self.client.get(stats_url, filters).json()
        self.assertEqual(after_stats["summary"], before_stats["summary"])
        self.assertEqual(after_stats["daily"], before_stats["daily"])
        response = self.client.get(reverse("gateway_admin:request-detail", kwargs={"request_id": record.pk}))
        self.assertEqual(response.status_code, 200)
        detail = response.json()["request"]
        self.assertIsNotNone(detail["hidden_at"])
        self.assertEqual(detail["detection_spans"][0]["id"], str(span.pk))
        self.assertEqual(detail["masking_mappings"][0]["id"], str(mapping.pk))
        self.assertNotIn("answer_text", detail)
        self.assertNotIn(record.answer_text, response.content.decode())
        self.assertTrue(SensitiveTerm.objects.filter(pk=term.pk).exists())

    def test_hidden_pending_request_cannot_be_previewed_or_decided_but_can_expire(self):
        record = self.make_request(
            input_text="가상 원문", state="awaiting_approval", inspection_status="complete",
            processing_route="external", approval_status="pending",
            approval_expires_at=timezone.now() + timedelta(minutes=30),
        )
        self.assertEqual(self.hide(record).status_code, 204)
        record.refresh_from_db()
        self.assertEqual(record.state, "awaiting_approval")
        self.assertEqual(record.approval_status, "pending")
        self.assert_error(self.client.get(self.url(record, "request-approval-preview")), 404, "REQUEST_NOT_FOUND")
        for decision in ("approved", "rejected"):
            response = self.client.post(
                self.url(record, "request-approval-decision"), json.dumps({"decision": decision}),
                content_type="application/json", **self.headers(),
            )
            self.assert_error(response, 404, "REQUEST_NOT_FOUND")
        Request.objects.filter(pk=record.pk).update(approval_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(expire_due_approvals(), 1)
        record.refresh_from_db()
        self.assertEqual(record.state, "expired")
        self.assertIsNone(record.decided_at)

    def test_hiding_approved_request_does_not_cancel_external_processing(self):
        record = self.make_request(
            state="processing", approval_status="approved", decided_at=timezone.now(),
            processing_route="external", external_send_started_at=timezone.now(), delivery_status="unknown",
        )
        before = Request.objects.values().get(pk=record.pk)
        self.assertEqual(self.hide(record).status_code, 204)
        after = Request.objects.values().get(pk=record.pk)
        for field in ("state", "approval_status", "decided_at", "processing_route", "external_send_started_at", "delivery_status", "finished_at"):
            self.assertEqual(after[field], before[field])

    def test_hidden_preparing_request_cannot_be_edited_or_reopened_by_intake_replay(self):
        payload = {"client_request_key": str(uuid4()), "instruction": "가상 최초 의뢰"}
        response = self.client.post(reverse("gateway:request-intake"), json.dumps(payload), content_type="application/json", **self.headers())
        self.assertEqual(response.status_code, 201)
        record = Request.objects.get(pk=response.json()["request"]["request_id"])
        self.assertEqual(self.hide(record).status_code, 204)
        response = self.client.patch(self.url(record, "request-intake-detail"), '{"instruction":"가상 수정"}', content_type="application/json", **self.headers())
        self.assert_error(response, 404, "REQUEST_NOT_FOUND")
        response = self.client.post(reverse("gateway:request-intake"), json.dumps(payload), content_type="application/json", **self.headers())
        self.assert_error(response, 409, "REQUEST_NOT_AVAILABLE")
        record.refresh_from_db()
        self.assertEqual(record.instruction, payload["instruction"])
        self.assertIsNotNone(record.hidden_at)
        self.assertEqual(Request.objects.count(), 1)

    def test_other_hidden_other_and_missing_requests_share_not_found_response(self):
        records = [self.make_request(user=self.other), self.make_request(user=self.other, hidden_at=timezone.now()), Request(request_id=uuid4())]
        responses = []
        for record in records:
            response = self.hide(record)
            self.assert_error(response, 404, "REQUEST_NOT_FOUND")
            responses.append(response.json())
        self.assertEqual(responses[0], responses[1])
        self.assertEqual(responses[1], responses[2])
        self.assertIsNone(Request.objects.get(pk=records[0].pk).hidden_at)

    def test_admin_and_operator_cannot_hide_even_their_own_legacy_requests(self):
        own = self.make_request()
        for user in (self.admin, self.operator):
            self.client.force_login(user)
            for record in (own, self.make_request(user=user)):
                self.assert_error(self.hide(record), 403, "FORBIDDEN")
                record.refresh_from_db()
                self.assertIsNone(record.hidden_at)

    def test_anonymous_expired_and_inactive_sessions_cannot_hide(self):
        record = self.make_request()
        self.client.logout()
        self.assert_error(self.hide(record), 403, "AUTH_REQUIRED")
        self.client.force_login(self.member)
        session = self.client.session
        session.set_expiry(timezone.now() - timedelta(seconds=1))
        session.save()
        self.assert_error(self.hide(record), 403, "AUTH_REQUIRED")
        self.client.force_login(self.member)
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.assert_error(self.hide(record), 403, "AUTH_REQUIRED")
        record.refresh_from_db()
        self.assertIsNone(record.hidden_at)

    def test_missing_and_conflicting_roles_cannot_hide(self):
        record = self.make_request()
        self.member.groups.clear()
        self.assert_error(self.hide(record), 403, "FORBIDDEN")
        self.member.groups.add(self.user_group, self.admin_group)
        self.assert_error(self.hide(record), 403, "FORBIDDEN")

    def test_service_rechecks_password_change_after_initial_authentication(self):
        record = self.make_request()
        changed = get_user_model().objects.get(pk=self.member.pk)
        changed.set_password("ChangedPass2!")
        changed.save(update_fields=["password"])
        with self.assertRaises(NotAuthenticated):
            hide_request(self.member, record.pk)
        record.refresh_from_db()
        self.assertIsNone(record.hidden_at)

    def test_csrf_is_required_and_failure_leaves_request_visible(self):
        record = self.make_request()
        response = self.client.post(self.url(record), "{}", content_type="application/json")
        self.assert_error(response, 403, "CSRF_FAILED")
        record.refresh_from_db()
        self.assertIsNone(record.hidden_at)

    def test_invalid_input_cannot_control_owner_state_or_restore_visibility(self):
        record = self.make_request()
        before = Request.objects.values().get(pk=record.pk)
        for data in (None, [], "", {"user_id": str(self.other.pk)}, {"hidden_at": None}, {"state": "failed"}):
            with self.subTest(data=data):
                response = self.client.post(self.url(record), json.dumps(data), content_type="application/json", **self.headers())
                self.assert_error(response, 400, "INVALID_INPUT")
        response = self.client.post(self.url(record) + "?hidden=false", "{}", content_type="application/json", **self.headers())
        self.assert_error(response, 400, "INVALID_INPUT")
        response = self.client.post(self.url(record), "{", content_type="application/json", **self.headers())
        self.assert_error(response, 400, "INVALID_JSON")
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_only_json_is_accepted_including_charset_parameter(self):
        record = self.make_request()
        for content_type in ("text/plain", "application/x-www-form-urlencoded"):
            response = self.client.post(self.url(record), "{}", content_type=content_type, **self.headers())
            self.assert_error(response, 415, "UNSUPPORTED_MEDIA_TYPE")
        response = self.client.post(self.url(record), {}, **self.headers())
        self.assert_error(response, 415, "UNSUPPORTED_MEDIA_TYPE")
        record.refresh_from_db()
        self.assertIsNone(record.hidden_at)
        response = self.client.post(self.url(record), "{}", content_type="application/json; charset=utf-8", **self.headers())
        self.assertEqual(response.status_code, 204)

    def test_unsupported_methods_do_not_hide(self):
        record = self.make_request()
        self.assert_error(self.client.get(self.url(record)), 405, "METHOD_NOT_ALLOWED")
        for method in (self.client.delete, self.client.patch, self.client.put):
            response = method(self.url(record), "{}", content_type="application/json", **self.headers())
            self.assert_error(response, 405, "METHOD_NOT_ALLOWED")
        record.refresh_from_db()
        self.assertIsNone(record.hidden_at)

    def test_database_error_rolls_back_without_exposing_details(self):
        record = self.make_request()
        before = Request.objects.values().get(pk=record.pk)
        original_update = QuerySet.update
        def update_then_fail(queryset, **kwargs):
            result = original_update(queryset, **kwargs)
            if queryset.model is Request:
                raise DatabaseError("PRIVATE_DB_DETAIL")
            return result
        with patch.object(QuerySet, "update", update_then_fail):
            response = self.hide(record)
        self.assert_error(response, 503, "REQUEST_HIDE_FAILED")
        self.assertNotIn("PRIVATE_DB_DETAIL", response.content.decode())
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)
