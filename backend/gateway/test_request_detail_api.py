"""가상 데이터로 본인 상세·완성 답변 조회와 내부 정보 비공개를 검증한다."""

import json
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
class RequestDetailAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.member = cls.account("USR64101", cls.user_group)
        cls.other = cls.account("USR64102", cls.user_group)
        cls.admin = cls.account("ADM64101", cls.admin_group)

    @classmethod
    def account(cls, username, group):
        user = get_user_model().objects.create_user(
            username=username, full_name="상세 검증용 가상 사용자", password="SecurePass1!",
        )
        user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.member)

    def make_request(self, user=None, **kwargs):
        return Request.objects.create(
            user=self.member if user is None else user, client_request_key=uuid4(), **kwargs,
        )

    def detail_url(self, record):
        return reverse("gateway:request-intake-detail", kwargs={"request_id": record.pk})

    def csrf_headers(self):
        token = self.client.get(reverse("accounts:csrf")).json()["csrf_token"]
        return {"HTTP_X_CSRFTOKEN": token}

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertNotIn("request", response.json())
        self.assertIn("no-store", response["Cache-Control"])

    def test_owner_reads_completed_text_request_without_changing_data(self):
        record = self.make_request(
            instruction="가상 문서를 요약해 주세요.", state=Request.State.COMPLETED,
            answer_text="  # 가상 완료 답변\n복원된 문장입니다. 😀\n",
            finished_at=timezone.now(),
        )
        before = Request.objects.values().get(pk=record.pk)
        response = self.client.get(self.detail_url(record))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"request": {
            "request_id": str(record.pk), "client_request_key": str(record.client_request_key),
            "instruction": record.instruction, "state": "completed",
            "created_at": record.created_at.isoformat(), "updated_at": record.updated_at.isoformat(),
            "processing_available": False, "document": None,
            "answer_text": record.answer_text, "finished_at": record.finished_at.isoformat(),
        }})
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)
        self.assertIn("no-store", response["Cache-Control"])

    def test_pdf_details_reuse_file_url_and_never_include_extracted_text_or_storage_key(self):
        record = self.make_request(instruction="가상 PDF를 요약해 주세요.")
        Document.objects.create(
            request=record, original_filename="가상자료.pdf", size_bytes=123, page_count=1,
            storage_object_key="private/fictional.pdf", extracted_text="가상 추출 원문",
            extraction_status=Document.ExtractionStatus.READY,
        )
        response = self.client.get(self.detail_url(record))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["request"]["document"], {
            "original_filename": "가상자료.pdf", "size_bytes": 123, "page_count": 1,
            "extraction_status": "ready",
            "file_url": reverse("gateway:request-file", kwargs={"request_id": record.pk}),
        })
        for text in ("private/fictional.pdf", "가상 추출 원문", "storage_object_key", "extracted_text"):
            self.assertNotIn(text, response.content.decode())

    def test_non_completed_states_never_expose_answer_even_if_stale_text_exists(self):
        for state in Request.State.values:
            if state == Request.State.COMPLETED:
                continue
            with self.subTest(state=state):
                record = self.make_request(state=state)
                # 모델 검증을 우회한 외부 저장 오류에도 미완성 답변은 노출하지 않는다.
                Request.objects.filter(pk=record.pk).update(answer_text="가상 미완성 답변")
                response = self.client.get(self.detail_url(record))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["request"]["state"], state)
                self.assertIsNone(response.json()["request"]["answer_text"])
                self.assertNotIn("가상 미완성 답변", response.content.decode())
                record.refresh_from_db()
                self.assertEqual(record.state, state)
                self.assertEqual(record.answer_text, "가상 미완성 답변")

    def test_completed_request_with_missing_or_blank_answer_returns_read_error(self):
        record = self.make_request(state=Request.State.COMPLETED)
        for answer in (None, "", " \n\t"):
            with self.subTest(answer=answer):
                Request.objects.filter(pk=record.pk).update(answer_text=answer)
                response = self.client.get(self.detail_url(record))
                self.assert_error(response, 503, "ANSWER_NOT_AVAILABLE")
                record.refresh_from_db()
                self.assertEqual(record.state, Request.State.COMPLETED)
                self.assertEqual(record.answer_text, answer)

    def test_other_hidden_and_missing_requests_share_not_found_response(self):
        other = self.make_request(user=self.other, state=Request.State.COMPLETED, answer_text="타인 답변")
        hidden = self.make_request(
            hidden_at=timezone.now(), state=Request.State.COMPLETED, answer_text="숨긴 답변",
        )
        urls = [self.detail_url(other), self.detail_url(hidden),
                reverse("gateway:request-intake-detail", kwargs={"request_id": uuid4()})]
        bodies = []
        for url in urls:
            response = self.client.get(url)
            self.assert_error(response, 404, "REQUEST_NOT_FOUND")
            bodies.append(response.json())
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[1], bodies[2])

    def test_admin_cannot_read_user_answer_or_own_legacy_request(self):
        records = [self.make_request(user=user, state=Request.State.COMPLETED, answer_text="가상 답변")
                   for user in (self.member, self.admin)]
        self.client.force_login(self.admin)
        for record in records:
            with self.subTest(owner=record.user_id):
                self.assert_error(self.client.get(self.detail_url(record)), 403, "FORBIDDEN")

    def test_anonymous_expired_and_inactive_sessions_are_denied(self):
        record = self.make_request(state=Request.State.COMPLETED, answer_text="가상 답변")
        self.client.logout()
        self.assert_error(self.client.get(self.detail_url(record)), 403, "AUTH_REQUIRED")
        self.client.force_login(self.member)
        session = self.client.session
        session.set_expiry(timezone.now() - timedelta(seconds=1))
        session.save()
        self.assert_error(self.client.get(self.detail_url(record)), 403, "AUTH_REQUIRED")
        self.client.force_login(self.member)
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.assert_error(self.client.get(self.detail_url(record)), 403, "AUTH_REQUIRED")

    def test_revoked_and_conflicting_roles_are_denied(self):
        record = self.make_request()
        self.member.groups.clear()
        self.assert_error(self.client.get(self.detail_url(record)), 403, "FORBIDDEN")
        self.member.groups.add(self.user_group, self.admin_group)
        self.assert_error(self.client.get(self.detail_url(record)), 403, "FORBIDDEN")

    def test_history_details_never_replace_approval_preview_or_expose_internal_data(self):
        term = SensitiveTerm.objects.create(value="가상비밀기업", label=SensitiveTerm.Label.COMPANY)
        for approval in Request.ApprovalStatus.values:
            with self.subTest(approval=approval):
                record = self.make_request(
                    instruction="가상 의뢰", input_text="가상비밀기업", approval_status=approval,
                    state=Request.State.AWAITING_APPROVAL,
                    warning_codes=["FAKE_WARNING"], error_code="private-error", error_stage="private-stage",
                    inspection_versions={"test": "fictional"}, intake_fingerprint="b" * 64,
                )
                mapping = MaskingMapping.objects.create(
                    request=record, sensitive_term=term, placeholder="FAKE_PRIVATE_TOKEN",
                )
                DetectionSpan.objects.create(
                    request=record, sensitive_term=term, mapping=mapping,
                    input_source=DetectionSpan.InputSource.INSTRUCTION, start_offset=0, end_offset=6,
                )
                response = self.client.get(self.detail_url(record))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(set(response.json()["request"]), {
                    "request_id", "client_request_key", "instruction", "state", "created_at",
                    "updated_at", "processing_available", "document", "answer_text", "finished_at",
                })
                for text in (term.value, mapping.placeholder, "private-error", "private-stage", "FAKE_WARNING"):
                    self.assertNotIn(text, response.content.decode())

    def test_database_failure_does_not_expose_internal_exception(self):
        record = self.make_request()
        with patch.object(Request.objects, "select_related", side_effect=DatabaseError("private DB details")):
            response = self.client.get(self.detail_url(record))
        self.assert_error(response, 503, "REQUEST_READ_FAILED")
        self.assertNotIn("private DB details", response.content.decode())

    def test_intake_update_and_completed_replay_keep_original_response_shape(self):
        payload = {"client_request_key": str(uuid4()), "instruction": "가상 최초 의뢰"}
        url = reverse("gateway:request-intake")
        created = self.client.post(
            url, json.dumps(payload), content_type="application/json", **self.csrf_headers(),
        )
        self.assertEqual(created.status_code, 201)
        record = Request.objects.get(pk=created.json()["request"]["request_id"])
        updated = self.client.patch(
            self.detail_url(record), json.dumps({"instruction": "가상 수정 의뢰"}),
            content_type="application/json", **self.csrf_headers(),
        )
        self.assertEqual(updated.status_code, 200)
        Request.objects.filter(pk=record.pk).update(state=Request.State.COMPLETED, answer_text="가상 완성 답변")
        replay = self.client.post(
            url, json.dumps(payload), content_type="application/json", **self.csrf_headers(),
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])
        for response in (created, updated, replay):
            self.assertNotIn("answer_text", response.json()["request"])
            self.assertNotIn("finished_at", response.json()["request"])
        detail = self.client.get(self.detail_url(record))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["request"]["answer_text"], "가상 완성 답변")
        self.assertEqual(Request.objects.count(), 1)
