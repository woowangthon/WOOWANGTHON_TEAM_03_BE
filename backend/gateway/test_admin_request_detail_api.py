"""가상 데이터로 관리자 상세의 원문·탐지·승인 조회와 답변 비공개를 검증한다."""

from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.roles import ADMIN_GROUP_NAME, USER_GROUP_NAME

from .models import DetectionSpan, Document, MaskingMapping, Request, SensitiveTerm
from .storage import document_storage
from .test_intake_api import pdf_bytes


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AdminRequestDetailAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin = cls.account("ADM65101", cls.admin_group)
        cls.member = cls.account("USR65101", cls.user_group)
        cls.other = cls.account("USR65102", cls.user_group)
        cls.operator = cls.account("OPS65101", is_staff=True, is_superuser=True)

    @classmethod
    def account(cls, username, group=None, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, full_name="관리 상세 검증용 가상 사용자", password="SecurePass1!", **kwargs,
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.admin)

    def make_request(self, user=None, **kwargs):
        return Request.objects.create(
            user=self.member if user is None else user, client_request_key=uuid4(), **kwargs,
        )

    def detail_url(self, record):
        return reverse("gateway_admin:request-detail", kwargs={"request_id": record.pk})

    def add_detection(self, record, term=None, start=0, mapped=True, placeholder="FAKE_TOKEN"):
        if term is None:
            term, _ = SensitiveTerm.objects.get_or_create(value="가상기업", label=SensitiveTerm.Label.COMPANY)
        mapping = None
        if mapped:
            mapping = MaskingMapping.objects.create(request=record, sensitive_term=term, placeholder=placeholder)
        span = DetectionSpan.objects.create(
            request=record, sensitive_term=term, mapping=mapping,
            input_source=DetectionSpan.InputSource.INSTRUCTION, start_offset=start, end_offset=start + 4,
        )
        return span, mapping

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertNotIn("request", response.json())
        self.assertIn("no-store", response["Cache-Control"])

    def test_admin_reads_text_request_and_stored_statuses_without_ai_answer(self):
        now = timezone.now()
        record = self.make_request(
            instruction="가상 의뢰문", input_text="가상 검사문", state="completed", answer_text="숨겨야 할 가상 AI 답변",
            inspection_status="complete", processing_route="external", warning_codes=["FAKE_WARNING"],
            inspection_versions={"detector": "fictional-v1"}, approval_status="approved",
            approval_expires_at=now + timedelta(minutes=30), decided_at=now,
            delivery_status="confirmed_sent", external_send_started_at=now,
            finished_at=now, intake_fingerprint="a" * 64,
        )
        before = Request.objects.values().get(pk=record.pk)
        response = self.client.get(self.detail_url(record))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"request": {
            "request_id": str(record.pk),
            "user": {"id": str(self.member.pk), "username": self.member.username,
                     "full_name": self.member.full_name, "is_active": True},
            "instruction": record.instruction, "input_text": record.input_text,
            "input_type": "text", "document": None, "state": "completed", "processing_route": "external",
            "created_at": record.created_at.isoformat(), "updated_at": record.updated_at.isoformat(),
            "finished_at": now.isoformat(), "hidden_at": None, "inspection_status": "complete",
            "detection_is_final": True, "detection_count": 0, "detection_labels": [],
            "detection_spans": [], "masking_mappings": [],
            "warning_codes": ["FAKE_WARNING"], "inspection_versions": {"detector": "fictional-v1"},
            "approval": {"status": "approved", "expires_at": record.approval_expires_at.isoformat(),
                         "decided_at": now.isoformat()},
            "delivery": {"status": "confirmed_sent", "external_send_started_at": now.isoformat()},
            "failure": {"stage": None, "code": None},
        }})
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)
        for value in ("answer_text", record.answer_text, "client_request_key", "intake_fingerprint",
                      self.member.password, "password"):
            self.assertNotIn(value, response.content.decode())
        self.assertIn("no-store", response["Cache-Control"])

    def test_pdf_metadata_and_extracted_text_use_existing_protected_file_api(self):
        record = self.make_request(instruction="가상 PDF 의뢰", hidden_at=timezone.now())
        content = pdf_bytes()
        with TemporaryDirectory() as root, override_settings(PRIVATE_DOCUMENT_ROOT=root):
            key = document_storage().save("requests/fictional/test.pdf", ContentFile(content))
            Document.objects.create(
                request=record, original_filename="가상자료.pdf", storage_object_key=key,
                size_bytes=len(content), page_count=1, extracted_text="가상 추출 본문",
                extraction_status=Document.ExtractionStatus.READY,
            )
            response = self.client.get(self.detail_url(record))
            self.assertEqual(response.status_code, 200)
            document = response.json()["request"]["document"]
            self.assertEqual(document, {
                "original_filename": "가상자료.pdf", "size_bytes": len(content), "page_count": 1,
                "extraction_status": "ready", "extracted_text": "가상 추출 본문",
                "file_url": reverse("gateway:request-file", kwargs={"request_id": record.pk}),
            })
            self.assertNotIn(key, response.content.decode())
            self.assertNotIn(root, response.content.decode())
            file_response = self.client.get(document["file_url"], HTTP_ACCEPT="application/pdf")
            self.assertEqual(file_response.status_code, 200)
            self.assertEqual(b"".join(file_response.streaming_content), content)
            self.client.force_login(self.other)
            self.assert_error(self.client.get(document["file_url"]), 404, "DOCUMENT_NOT_FOUND")

    def test_hidden_inactive_and_renamed_owners_are_still_readable(self):
        record = self.make_request(hidden_at=timezone.now())
        get_user_model().objects.filter(pk=self.member.pk).update(full_name="변경된 가상 이름", is_active=False)
        self.member.groups.clear()
        response = self.client.get(self.detail_url(record))
        self.assertEqual(response.status_code, 200)
        data = response.json()["request"]
        self.assertEqual(data["hidden_at"], record.hidden_at.isoformat())
        self.assertEqual(data["user"]["full_name"], "변경된 가상 이름")
        self.assertFalse(data["user"]["is_active"])

    def test_only_requested_spans_and_mappings_are_returned_in_coordinate_order(self):
        record = self.make_request(input_text="😀 가상기업 관련 가상 검사문", inspection_status="complete")
        later, mapping = self.add_detection(record, start=10)
        earlier, _ = self.add_detection(record, term=later.sensitive_term, start=2, mapped=False)
        other = self.make_request(user=self.other, inspection_status="complete")
        other_span, other_mapping = self.add_detection(other, term=later.sensitive_term, placeholder="OTHER_PRIVATE_TOKEN")
        response = self.client.get(self.detail_url(record))
        self.assertEqual(response.status_code, 200)
        data = response.json()["request"]
        self.assertEqual(data["input_text"], record.input_text)
        self.assertEqual(data["detection_count"], 2)
        self.assertEqual(data["detection_labels"], ["company"])
        self.assertEqual(data["detection_spans"], [{
            "id": str(span.pk), "sensitive_term_id": str(span.sensitive_term_id),
            "value": "가상기업", "label": "company", "input_source": "instruction",
            "start_offset": span.start_offset, "end_offset": span.end_offset,
            "mapping_id": str(span.mapping_id) if span.mapping_id else None,
        } for span in (earlier, later)])
        self.assertEqual(data["masking_mappings"], [{
            "id": str(mapping.pk), "sensitive_term_id": str(mapping.sensitive_term_id),
            "value": "가상기업", "label": "company", "placeholder": "FAKE_TOKEN",
        }])
        for value in (str(other.pk), str(other_span.pk), str(other_mapping.pk), other_mapping.placeholder):
            self.assertNotIn(value, response.content.decode())

    def test_partial_inspection_data_is_marked_non_final_not_zero(self):
        for status in ("pending", "degraded", "failed"):
            with self.subTest(status=status):
                record = self.make_request(inspection_status=status, state="failed", error_stage="inspection",
                                           error_code="FAKE_INSPECTION_ERROR")
                self.add_detection(record)
                response = self.client.get(self.detail_url(record))
                self.assertEqual(response.status_code, 200)
                data = response.json()["request"]
                self.assertFalse(data["detection_is_final"])
                self.assertIsNone(data["detection_count"])
                self.assertIsNone(data["detection_labels"])
                self.assertEqual(len(data["detection_spans"]), 1)
                self.assertEqual(data["inspection_status"], status)
                self.assertEqual(data["failure"], {"stage": "inspection", "code": "FAKE_INSPECTION_ERROR"})

    def test_approval_end_does_not_hide_mappings_from_admin(self):
        for approval in ("approved", "rejected", "expired"):
            with self.subTest(approval=approval):
                record = self.make_request(inspection_status="complete", approval_status=approval)
                _, mapping = self.add_detection(record)
                data = self.client.get(self.detail_url(record)).json()["request"]
                self.assertEqual(data["approval"]["status"], approval)
                self.assertEqual(data["masking_mappings"][0]["id"], str(mapping.pk))
                self.assertIsNone(data["delivery"]["status"])
                self.assertIsNone(data["delivery"]["external_send_started_at"])

    def test_read_does_not_expire_pending_approval_or_invent_delivery_confirmation(self):
        record = self.make_request(
            inspection_status="complete", state="awaiting_approval", approval_status="pending",
            approval_expires_at=timezone.now() - timedelta(minutes=1), processing_route="external",
            delivery_status="unknown", external_send_started_at=timezone.now(),
        )
        self.add_detection(record)
        before = Request.objects.values().get(pk=record.pk)
        data = self.client.get(self.detail_url(record)).json()["request"]
        self.assertEqual(data["state"], "awaiting_approval")
        self.assertEqual(data["approval"]["status"], "pending")
        self.assertEqual(data["delivery"]["status"], "unknown")
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_missing_saved_values_remain_null_and_ai_answer_is_unnecessary(self):
        record = self.make_request(state="completed", answer_text=None)
        Document.objects.create(request=record, original_filename="가상대기.pdf", extraction_status="pending")
        response = self.client.get(self.detail_url(record))
        self.assertEqual(response.status_code, 200)
        data = response.json()["request"]
        self.assertIsNone(data["document"]["extracted_text"])
        self.assertIsNone(data["document"]["page_count"])
        self.assertIsNone(data["finished_at"])
        self.assertEqual(data["failure"], {"stage": None, "code": None})
        self.assertNotIn("answer_text", data)

    def test_owner_other_user_anonymous_and_operator_are_denied(self):
        record = self.make_request()
        self.client.logout()
        self.assert_error(self.client.get(self.detail_url(record)), 403, "AUTH_REQUIRED")
        for actor in (self.member, self.other, self.operator):
            with self.subTest(actor=actor.username):
                self.client.force_login(actor)
                with patch("gateway.admin_views.get_admin_request_detail_response") as reader:
                    self.assert_error(self.client.get(self.detail_url(record)), 403, "FORBIDDEN")
                reader.assert_not_called()

    def test_revoked_and_conflicting_admin_roles_are_denied(self):
        record = self.make_request()
        self.admin.groups.clear()
        self.assert_error(self.client.get(self.detail_url(record)), 403, "FORBIDDEN")
        self.admin.groups.add(self.admin_group, self.user_group)
        self.assert_error(self.client.get(self.detail_url(record)), 403, "FORBIDDEN")

    def test_expired_and_deactivated_admin_sessions_are_denied(self):
        record = self.make_request()
        session = self.client.session
        session.set_expiry(timezone.now() - timedelta(seconds=1))
        session.save()
        self.assert_error(self.client.get(self.detail_url(record)), 403, "AUTH_REQUIRED")
        self.client.force_login(self.admin)
        get_user_model().objects.filter(pk=self.admin.pk).update(is_active=False)
        self.assert_error(self.client.get(self.detail_url(record)), 403, "AUTH_REQUIRED")

    def test_missing_request_and_query_parameters_do_not_expose_extra_data(self):
        missing_url = reverse("gateway_admin:request-detail", kwargs={"request_id": uuid4()})
        self.assert_error(self.client.get(missing_url), 404, "REQUEST_NOT_FOUND")
        record = self.make_request()
        for params in ({"include_answer": "true"}, {"user_id": str(self.other.pk)}, {"page": "1"}):
            with self.subTest(params=params):
                self.assert_error(self.client.get(self.detail_url(record), params), 400, "INVALID_INPUT")

    def test_database_failure_at_any_read_stage_returns_error_not_partial_success(self):
        record = self.make_request()
        for name in ("get_admin_request_record", "get_admin_detection_spans", "get_admin_masking_mappings"):
            with self.subTest(stage=name):
                with patch(f"gateway.admin_request_detail.{name}", side_effect=DatabaseError("private DB details")):
                    response = self.client.get(self.detail_url(record))
                self.assert_error(response, 503, "ADMIN_REQUEST_READ_FAILED")
                self.assertNotIn("private DB details", response.content.decode())

    def test_list_and_detail_final_detection_counts_agree(self):
        record = self.make_request(inspection_status="complete")
        self.add_detection(record)
        self.add_detection(record, start=10, mapped=False)
        person = SensitiveTerm.objects.create(value="가상사람", label="person")
        self.add_detection(record, term=person, start=20, placeholder="FAKE_PERSON_TOKEN")
        listed = self.client.get(reverse("gateway_admin:request-list")).json()["results"][0]
        detail = self.client.get(self.detail_url(record)).json()["request"]
        self.assertEqual(listed["detection_count"], detail["detection_count"])
        self.assertEqual(listed["detection_labels"], detail["detection_labels"])
        self.assertEqual(detail["detection_count"], 3)

    def test_detail_is_read_only_even_with_valid_csrf(self):
        record = self.make_request(instruction="변경 금지 가상 의뢰")
        token = self.client.get(reverse("accounts:csrf")).json()["csrf_token"]
        for method in ("post", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    self.detail_url(record), "{}", content_type="application/json", HTTP_X_CSRFTOKEN=token,
                )
                self.assert_error(response, 405, "METHOD_NOT_ALLOWED")
        record.refresh_from_db()
        self.assertEqual(record.instruction, "변경 금지 가상 의뢰")
