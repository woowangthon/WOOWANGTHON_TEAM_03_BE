"""가상 검사 결과로 승인 전 조회의 권한·기한·정보 제한을 검증한다. AI는 호출하지 않는다."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.roles import ADMIN_GROUP_NAME, USER_GROUP_NAME

from .models import DetectionSpan, Document, MaskingMapping, Request, SensitiveTerm


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ApprovalPreviewAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.member = cls.account("USR63101", cls.user_group)
        cls.other = cls.account("USR63102", cls.user_group)
        cls.admin = cls.account("ADM63101", cls.admin_group)

    @classmethod
    def account(cls, username, group):
        user = get_user_model().objects.create_user(
            username=username, full_name="사전 확인용 가상 사용자", password="SecurePass1!",
        )
        user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.member)

    def make_request(self, **kwargs):
        data = {
            "user": self.member, "client_request_key": uuid4(),
            "instruction": "가상 문서를 요약해 주세요.", "input_text": "가상 검사 원문",
            "state": Request.State.AWAITING_APPROVAL,
            "inspection_status": Request.InspectionStatus.COMPLETE,
            "processing_route": Request.ProcessingRoute.EXTERNAL,
            "approval_status": Request.ApprovalStatus.PENDING,
            "approval_expires_at": timezone.now() + timedelta(minutes=30),
        }
        data.update(kwargs)
        return Request.objects.create(**data)

    def url(self, record):
        return reverse("gateway:request-approval-preview", kwargs={"request_id": record.pk})

    def add_span(self, record, value="가상", start=0, end=2, source="instruction", label="company"):
        term, _ = SensitiveTerm.objects.get_or_create(value=value, label=label)
        return DetectionSpan.objects.create(
            request=record, sensitive_term=term, input_source=source,
            start_offset=start, end_offset=end,
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertNotIn("preview", response.json())
        self.assertNotIn("input_text", response.content.decode())
        self.assertIn("no-store", response["Cache-Control"])

    def test_owner_receives_original_text_and_ordered_categories_without_internal_fields(self):
        text = "  가상기업에 문의\n\n😀 가상직원\r\n"
        record = self.make_request(input_text=text, warning_codes=["PRIVATE_WARNING"])
        Document.objects.create(
            request=record, original_filename="가상.pdf", storage_object_key="private/not-public.pdf",
            extracted_text="😀 가상직원\r\n", extraction_status=Document.ExtractionStatus.READY,
        )
        # 저장 순서와 무관하게 좌표순으로 반환한다. 공백·개행·이모지를 변경하지 않는다.
        person = self.add_span(record, "가상직원", text.index("가상직원"), text.index("가상직원") + 4, "pdf", "person")
        company = self.add_span(record, "가상기업", 2, 6)
        mapping = MaskingMapping.objects.create(
            request=record, sensitive_term=company.sensitive_term, placeholder="PRIVATE_RESTORE_TOKEN",
        )
        company.mapping = mapping
        company.save()
        response = self.client.get(self.url(record))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"preview": {
            "request_id": str(record.pk), "state": "awaiting_approval",
            "inspection_status": "complete", "processing_route": "external", "input_text": text,
            "detection_count": 2,
            "detection_spans": [
                {"id": str(company.pk), "input_source": "instruction", "start_offset": 2, "end_offset": 6, "label": "company"},
                {"id": str(person.pk), "input_source": "pdf", "start_offset": person.start_offset, "end_offset": person.end_offset, "label": "person"},
            ],
            "approval": {"status": "pending", "expires_at": record.approval_expires_at.isoformat()},
        }})
        self.assertIn("no-store", response["Cache-Control"])
        for secret in ("PRIVATE_RESTORE_TOKEN", "PRIVATE_WARNING", "private/not-public.pdf", str(mapping.pk), str(company.sensitive_term_id)):
            self.assertNotIn(secret, response.content.decode())

    def test_text_only_with_zero_detections_still_requires_approval_and_does_not_mutate(self):
        record = self.make_request(delivery_status=Request.DeliveryStatus.NOT_SENT)
        before = Request.objects.values().get(pk=record.pk)
        for _ in range(2):
            response = self.client.get(self.url(record))
            self.assertEqual(response.status_code, 200)
            preview = response.json()["preview"]
            self.assertEqual(preview["detection_count"], 0)
            self.assertEqual(preview["detection_spans"], [])
            self.assertEqual(preview["approval"]["status"], "pending")
            self.assertEqual(preview["approval"]["expires_at"], record.approval_expires_at.isoformat())
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_other_requests_do_not_contribute_spans(self):
        own = self.make_request()
        other = self.make_request(user=self.other)
        self.add_span(other, value="타인비밀")
        response = self.client.get(self.url(own))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["preview"]["detection_spans"], [])
        self.assertNotIn("타인비밀", response.content.decode())

    def test_other_hidden_and_missing_requests_share_not_found_response(self):
        records = [self.make_request(user=self.other), self.make_request(hidden_at=timezone.now())]
        urls = [self.url(record) for record in records]
        urls.append(reverse("gateway:request-approval-preview", kwargs={"request_id": uuid4()}))
        bodies = []
        for url in urls:
            response = self.client.get(url)
            self.assert_error(response, 404, "REQUEST_NOT_FOUND")
            bodies.append(response.json())
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[1], bodies[2])

    def test_admin_cannot_read_preview_including_own_legacy_request(self):
        self.client.force_login(self.admin)
        for owner in (self.member, self.admin):
            self.assert_error(self.client.get(self.url(self.make_request(user=owner))), 403, "FORBIDDEN")

    def test_anonymous_expired_and_inactive_sessions_are_denied(self):
        record = self.make_request()
        self.client.logout()
        self.assert_error(self.client.get(self.url(record)), 403, "AUTH_REQUIRED")
        self.client.force_login(self.member)
        session = self.client.session
        session.set_expiry(timezone.now() - timedelta(seconds=1))
        session.save()
        self.assert_error(self.client.get(self.url(record)), 403, "AUTH_REQUIRED")
        self.client.force_login(self.member)
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.assert_error(self.client.get(self.url(record)), 403, "AUTH_REQUIRED")

    def test_missing_or_conflicting_service_roles_are_denied(self):
        record = self.make_request()
        self.member.groups.clear()
        self.assert_error(self.client.get(self.url(record)), 403, "FORBIDDEN")
        self.member.groups.add(self.user_group, self.admin_group)
        self.assert_error(self.client.get(self.url(record)), 403, "FORBIDDEN")

    def test_only_awaiting_approval_state_is_available(self):
        for state in Request.State.values:
            if state == Request.State.AWAITING_APPROVAL:
                continue
            with self.subTest(state=state):
                record = self.make_request(state=state)
                self.assert_error(self.client.get(self.url(record)), 409, "APPROVAL_NOT_AVAILABLE")

    def test_incomplete_inspection_or_non_external_route_is_denied(self):
        cases = [{"inspection_status": value} for value in Request.InspectionStatus.values if value != "complete"]
        cases += [{"processing_route": value} for value in (None, "local", "blocked")]
        for fields in cases:
            with self.subTest(fields=fields):
                record = self.make_request(**fields)
                self.assert_error(self.client.get(self.url(record)), 409, "APPROVAL_NOT_AVAILABLE")

    def test_decided_or_sent_records_are_denied_even_if_state_is_stale(self):
        cases = [{"approval_status": value} for value in (None, "approved", "rejected", "expired")]
        cases += [{"delivery_status": value} for value in ("unknown", "confirmed_sent")]
        cases += [{field: timezone.now()} for field in ("decided_at", "finished_at", "external_send_started_at")]
        for fields in cases:
            with self.subTest(fields=fields):
                record = self.make_request(**fields)
                self.assert_error(self.client.get(self.url(record)), 409, "APPROVAL_NOT_AVAILABLE")

    def test_expiry_boundary_and_elapsed_deadline_deny_without_mutation(self):
        now = timezone.now()
        for seconds in (-1, 0, 1):
            with self.subTest(seconds=seconds):
                record = self.make_request(approval_expires_at=now + timedelta(seconds=seconds))
                before = Request.objects.values().get(pk=record.pk)
                with patch("gateway.approval_preview.timezone.now", return_value=now):
                    response = self.client.get(self.url(record))
                if seconds <= 0:
                    self.assert_error(response, 409, "APPROVAL_EXPIRED")
                else:
                    self.assertEqual(response.status_code, 200)
                self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_expiry_during_span_read_does_not_return_original_text(self):
        record = self.make_request()
        from .approval_preview import get_preview_spans

        def read_at_deadline(current):
            spans = get_preview_spans(current)
            clock.return_value = record.approval_expires_at
            return spans

        with patch("gateway.approval_preview.timezone.now", return_value=timezone.now()) as clock:
            with patch("gateway.approval_preview.get_preview_spans", side_effect=read_at_deadline):
                response = self.client.get(self.url(record))
        self.assert_error(response, 409, "APPROVAL_EXPIRED")

    def test_missing_deadline_or_original_text_is_not_treated_as_zero_detections(self):
        for fields in ({"approval_expires_at": None}, {"input_text": ""}, {"input_text": " \n\t"}):
            with self.subTest(fields=fields):
                record = self.make_request(**fields)
                self.assert_error(self.client.get(self.url(record)), 503, "PREVIEW_NOT_AVAILABLE")

    def test_corrupted_overlap_or_unknown_category_is_not_exposed(self):
        record = self.make_request(input_text="가상기업 가상직원")
        self.add_span(record, "가상기업", 0, 4)
        person = self.add_span(record, "가상직원", 5, 9, label="person")
        # 외부 저장자가 모델 검증을 우회한 경우에도 잘못된 구간을 최종 결과로 반환하지 않는다.
        DetectionSpan.objects.filter(pk=person.pk).update(start_offset=3)
        self.assert_error(self.client.get(self.url(record)), 503, "PREVIEW_NOT_AVAILABLE")
        DetectionSpan.objects.filter(pk=person.pk).update(start_offset=5, input_source="unknown")
        self.assert_error(self.client.get(self.url(record)), 503, "PREVIEW_NOT_AVAILABLE")
        DetectionSpan.objects.filter(pk=person.pk).update(input_source="instruction")
        SensitiveTerm.objects.filter(pk=person.sensitive_term_id).update(label="unknown")
        self.assert_error(self.client.get(self.url(record)), 503, "PREVIEW_NOT_AVAILABLE")

    def test_all_supported_categories_are_returned_as_stored(self):
        for label in SensitiveTerm.Label.values:
            with self.subTest(label=label):
                record = self.make_request()
                self.add_span(record, label=label)
                response = self.client.get(self.url(record))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["preview"]["detection_spans"][0]["label"], label)

    def test_query_parameters_and_mutating_methods_are_rejected(self):
        record = self.make_request()
        self.assert_error(self.client.get(self.url(record), {"include_mappings": "true"}), 400, "INVALID_INPUT")
        token = self.client.get(reverse("accounts:csrf")).json()["csrf_token"]
        for method in (self.client.post, self.client.patch, self.client.put, self.client.delete):
            response = method(self.url(record), data="{}", content_type="application/json", HTTP_X_CSRFTOKEN=token)
            self.assert_error(response, 405, "METHOD_NOT_ALLOWED")

    def test_database_error_does_not_disclose_internal_details(self):
        record = self.make_request()
        with patch.object(Request.objects, "select_for_update", side_effect=DatabaseError("PRIVATE_DB_ERROR")):
            response = self.client.get(self.url(record))
        self.assert_error(response, 503, "REQUEST_READ_FAILED")
        self.assertNotIn("PRIVATE_DB_ERROR", response.content.decode())
