"""가상 계정·PDF 로 처리 시작 API 와 승인 뒤 외부 전송 연결, 답변 조회까지의 연결을 검증한다. 실제 AI는 호출하지 않는다."""

import json
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.roles import ADMIN_GROUP_NAME, USER_GROUP_NAME

from . import processing
from .models import DetectionSpan, Document, MaskingMapping, Request, SensitiveTerm
from .test_processing import FakeClient, blank_pdf, text_pdf


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    GATEWAY_WORKER="command",
    MODEL_SERVICE_URL="http://model.invalid",
)
class ProcessingAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.member = cls.account("USR70101", cls.user_group)
        cls.other = cls.account("USR70102", cls.user_group)
        cls.admin = cls.account("ADM70101", cls.admin_group)

    @classmethod
    def account(cls, username, group):
        user = get_user_model().objects.create_user(
            username=username, full_name="처리 API 검증용 가상 사용자", password="SecurePass1!",
        )
        user.groups.add(group)
        return user

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        storage_settings = override_settings(PRIVATE_DOCUMENT_ROOT=self.tmp.name)
        storage_settings.enable()
        self.addCleanup(storage_settings.disable)
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.member)

    def headers(self):
        token = self.client.get(reverse("accounts:csrf")).json()["csrf_token"]
        return {"HTTP_X_CSRFTOKEN": token}

    def intake(self, instruction=None, pdf=None):
        values = {"client_request_key": str(uuid4())}
        if instruction is not None:
            values["instruction"] = instruction
        if pdf is not None:
            values["pdf"] = SimpleUploadedFile("fictional.pdf", pdf, "application/pdf")
            response = self.client.post(reverse("gateway:request-intake"), values, **self.headers())
        else:
            response = self.client.post(
                reverse("gateway:request-intake"), json.dumps(values), content_type="application/json", **self.headers(),
            )
        self.assertEqual(response.status_code, 201, response.content[:300])
        return response.json()["request"]

    def start(self, request_id, body=None, query="", with_token=True):
        url = reverse("gateway:request-start", kwargs={"request_id": request_id}) + query
        headers = self.headers() if with_token else {}
        data = "" if body is None else json.dumps(body)
        return self.client.post(url, data, content_type="application/json", **headers)

    def detail(self, request_id):
        return self.client.get(reverse("gateway:request-intake-detail", kwargs={"request_id": request_id}))

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content[:300])
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertNotIn("request", response.json())

    def test_text_request_is_queued_and_analysis_scheduled(self):
        created = self.intake("가상 문서를 요약해 주세요.")
        self.assertTrue(created["processing_available"])
        with patch.object(processing, "launch") as launch:
            response = self.start(created["request_id"])
        self.assertEqual(response.status_code, 202, response.content[:300])
        data = response.json()["request"]
        self.assertEqual(data["state"], "queued")
        self.assertFalse(data["processing_available"])
        self.assertEqual(set(data), {
            "request_id", "client_request_key", "instruction", "state", "created_at", "updated_at",
            "processing_available", "document",
        })
        launch.assert_called_once_with(processing.run_analysis, UUID(created["request_id"]))
        record = Request.objects.get(pk=created["request_id"])
        self.assertEqual(record.input_text, "가상 문서를 요약해 주세요.")
        self.assertIn("no-store", response["Cache-Control"])

    def test_pdf_request_is_extracted_on_start_without_exposing_text(self):
        created = self.intake("요약해 주세요.", text_pdf())
        with patch.object(processing, "launch"):
            response = self.start(created["request_id"])
        self.assertEqual(response.status_code, 202, response.content[:300])
        document = Document.objects.get(pk=created["request_id"])
        self.assertEqual(document.extraction_status, Document.ExtractionStatus.READY)
        self.assertIn("NCR-2026-0001", document.extracted_text)
        self.assertEqual(response.json()["request"]["document"]["extraction_status"], "ready")
        self.assertNotIn("NCR-2026-0001", response.content.decode())
        detail = self.detail(created["request_id"])
        self.assertEqual(detail.json()["request"]["state"], "queued")
        self.assertNotIn("NCR-2026-0001", detail.content.decode())

    def test_pdf_only_request_needs_instruction_first(self):
        created = self.intake(pdf=text_pdf())
        self.assertFalse(created["processing_available"])
        with patch.object(processing, "launch") as launch:
            self.assert_error(self.start(created["request_id"]), 400, "INSTRUCTION_REQUIRED")
            launch.assert_not_called()
            patched = self.client.patch(
                reverse("gateway:request-intake-detail", kwargs={"request_id": created["request_id"]}),
                json.dumps({"instruction": "이 PDF를 요약해 주세요."}), content_type="application/json", **self.headers(),
            )
            self.assertEqual(patched.status_code, 200)
            self.assertTrue(patched.json()["request"]["processing_available"])
            self.assertEqual(self.start(created["request_id"]).status_code, 202)
            launch.assert_called_once()

    def test_start_is_refused_for_wrong_users_states_and_inputs(self):
        created = self.intake("가상 의뢰")
        request_id = created["request_id"]
        with patch.object(processing, "launch") as launch:
            self.assert_error(self.start(request_id, with_token=False), 403, "CSRF_FAILED")
            self.assert_error(self.start(request_id, body={"instruction": "바꾸기"}), 400, "INVALID_INPUT")
            self.assert_error(self.start(request_id, query="?force=1"), 400, "INVALID_INPUT")
            self.assert_error(self.start(uuid4()), 404, "REQUEST_NOT_FOUND")
            self.client.force_login(self.other)
            self.assert_error(self.start(request_id), 404, "REQUEST_NOT_FOUND")
            self.client.force_login(self.admin)
            self.assert_error(self.start(request_id), 403, "FORBIDDEN")
            self.client.logout()
            self.assert_error(self.start(request_id), 403, "AUTH_REQUIRED")  # 세션 인증은 401 대신 403(접수 API 와 같다)
            launch.assert_not_called()
            self.client.force_login(self.member)
            self.assertEqual(self.start(request_id).status_code, 202)
            self.assert_error(self.start(request_id), 409, "PROCESSING_ALREADY_STARTED")
            launch.assert_called_once()
        self.assertEqual(Request.objects.get(pk=request_id).state, Request.State.QUEUED)

    def test_pdf_without_text_fails_request_visibly(self):
        created = self.intake("요약", blank_pdf())
        with patch.object(processing, "launch") as launch:
            self.assert_error(self.start(created["request_id"]), 400, "PDF_NO_TEXT")
        launch.assert_not_called()
        data = self.detail(created["request_id"]).json()["request"]
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["document"]["extraction_status"], "failed")
        self.assertFalse(data["processing_available"])

    def awaiting_request(self):
        record = Request.objects.create(
            user=self.member, client_request_key=uuid4(), intake_fingerprint="a" * 64,
            instruction="가상 의뢰", input_text="가상 의뢰\n\n가상기업 견적 문의",
            state=Request.State.AWAITING_APPROVAL, inspection_status=Request.InspectionStatus.COMPLETE,
            processing_route=Request.ProcessingRoute.EXTERNAL, approval_status=Request.ApprovalStatus.PENDING,
            approval_expires_at=timezone.now() + timedelta(minutes=30), delivery_status=Request.DeliveryStatus.NOT_SENT,
        )
        Document.objects.create(
            request=record, original_filename="fictional.pdf", extracted_text="가상기업 견적 문의",
            extraction_status=Document.ExtractionStatus.READY,
        )
        term, _ = SensitiveTerm.objects.get_or_create(value="가상기업", label=SensitiveTerm.Label.COMPANY)
        mapping = MaskingMapping.objects.create(request=record, sensitive_term=term, placeholder="[[ORG_1]]")
        start = record.input_text.index("가상기업")
        DetectionSpan.objects.create(
            request=record, sensitive_term=term, mapping=mapping, input_source=DetectionSpan.InputSource.PDF,
            start_offset=start, end_offset=start + 4,
        )
        return record

    def decide(self, record, decision):
        return self.client.post(
            reverse("gateway:request-approval-decision", kwargs={"request_id": record.pk}),
            json.dumps({"decision": decision}), content_type="application/json", **self.headers(),
        )

    def test_approval_schedules_external_send_and_rejection_does_not(self):
        approved = self.awaiting_request()
        rejected = self.awaiting_request()
        with patch("gateway.views.launch") as launch:
            response = self.decide(approved, "approved")
            self.assertEqual(response.status_code, 200, response.content[:300])
            self.assertEqual(response.json()["request"]["state"], "queued")
            launch.assert_called_once_with(processing.run_external, approved.pk)
            launch.reset_mock()
            self.assertEqual(self.decide(rejected, "rejected").status_code, 200)
            launch.assert_not_called()

    def test_completed_answer_is_restored_and_visible_in_detail(self):
        record = self.awaiting_request()
        self.assertEqual(self.decide(record, "approved").status_code, 200)
        client = FakeClient(answer_external={
            "restored": "가상기업의 견적 문의에 대한 가상 답변입니다.", "answer": "[[ORG_1]]의 견적 문의에 대한 가상 답변입니다.",
            "unresolved": [], "stop_reason": "end_turn", "truncated": False, "refusal": None,
        })
        with patch.object(processing, "model_client", return_value=client):
            processing.run_external(record.pk)
        self.assertEqual(client.calls[0][1]["masked"], "가상 의뢰\n\n[[ORG_1]] 견적 문의")
        self.assertEqual(client.calls[0][1]["mapping"], {"[[ORG_1]]": "가상기업"})
        data = self.detail(record.pk).json()["request"]
        self.assertEqual(data["state"], "completed")
        self.assertEqual(data["answer_text"], "가상기업의 견적 문의에 대한 가상 답변입니다.")
        self.assertIsNotNone(data["finished_at"])
        record.refresh_from_db()
        self.assertEqual(record.delivery_status, Request.DeliveryStatus.CONFIRMED_SENT)
