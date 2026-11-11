"""가상 데이터와 가짜 모델 클라이언트로 처리 시작·검사 결과 저장·승인 뒤 외부 전송을 검증한다. 실제 AI는 호출하지 않는다.

testdata/ 의 응답 예시는 AI 저장소 docs/samples 의 가상 문서(D2~D4)로 만든 실제 응답 형식이다."""

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from uuid import uuid4

import pymupdf
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from accounts.roles import ADMIN_GROUP_NAME, USER_GROUP_NAME

from . import processing
from .approval_decision import decide_approval
from .approval_preview import get_approval_preview
from .intake_validation import IntakeError
from .model_client import LeakBlocked, Unavailable
from .models import DetectionSpan, Document, MaskingMapping, Request, SensitiveTerm
from .request_visibility import hide_request
from .storage import document_storage

SAMPLES = Path(__file__).resolve().parent / "testdata"


def sample(name):
    with open(SAMPLES / f"{name}.json", encoding="utf-8") as handle:
        return json.load(handle)


def split_sample_text(name):
    """예시 요청의 text 를 의뢰문과 본문으로 나눈다(의뢰문 + 빈 줄 + 본문 규칙)."""
    instruction, separator, body = sample(name)["request"]["text"].partition(processing.BODY_SEP)
    assert separator and body, "예시 text 가 의뢰문+본문 형식이 아니다"
    return instruction, body


def text_pdf(lines=("Fictional Corp report NCR-2026-0001", "Contact: 010-0000-0000")):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "\n".join(lines))
    data = doc.tobytes()
    doc.close()
    return data


def blank_pdf():
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


class FakeClient:
    """경로별 응답(또는 예외)을 미리 넣어 두고 호출 내용을 기록한다. 응답이 목록이면 호출마다 차례로 하나씩 쓴다.
    during 에 경로별 함수를 주면 응답을 돌려주기 직전에 실행한다(모델 호출 도중에 다른 쪽이 의뢰를 바꾸는 상황)."""

    def __init__(self, during=None, **responses):
        self.responses = responses
        self.during = during or {}
        self.calls = []

    def _answer(self, name, payload):
        self.calls.append((name, payload))
        result = self.responses.get(name)
        if isinstance(result, list):
            result = result.pop(0)
        if name in self.during:
            self.during[name]()
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise AssertionError(f"예상하지 않은 호출: {name}")
        return copy.deepcopy(result)

    def analyze(self, **payload):
        return self._answer("analyze", payload)

    def answer_local(self, **payload):
        return self._answer("answer_local", payload)

    def answer_external(self, **payload):
        return self._answer("answer_external", payload)


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    GATEWAY_WORKER="command",  # 테스트에서는 배경 스레드를 띄우지 않고 작업 함수를 직접 부른다
    MODEL_SERVICE_URL="http://model.invalid",
)
class ProcessingTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.member = cls.account("USR70001", cls.user_group)
        cls.other = cls.account("USR70002", cls.user_group)
        cls.admin = cls.account("ADM70001", cls.admin_group)

    @classmethod
    def account(cls, username, group):
        user = get_user_model().objects.create_user(
            username=username, full_name="처리 검증용 가상 사용자", password="SecurePass1!",
        )
        user.groups.add(group)
        return user

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        storage_settings = override_settings(PRIVATE_DOCUMENT_ROOT=self.tmp.name)
        storage_settings.enable()
        self.addCleanup(storage_settings.disable)

    def make_request(self, instruction="가상 의뢰문입니다.", user=None, **kwargs):
        return Request.objects.create(
            user=user or self.member, client_request_key=uuid4(), intake_fingerprint="a" * 64,
            instruction=instruction, **kwargs,
        )

    def attach_pdf(self, record, data=None, status=Document.ExtractionStatus.PENDING, **kwargs):
        key = None
        if data is not None:
            key = document_storage().save(f"requests/{record.pk}/{uuid4().hex}.pdf", ContentFile(data))
        return Document.objects.create(
            request=record, original_filename="fictional.pdf", storage_object_key=key,
            size_bytes=len(data) if data else None, page_count=1 if data else None, extraction_status=status, **kwargs,
        )

    def queued_sample(self, name="analyze_D3", **kwargs):
        """예시 응답의 text 와 같은 input_text 를 가진 검사 대기 의뢰(PDF 포함)."""
        instruction, body = split_sample_text(name)
        record = self.make_request(instruction, state=Request.State.QUEUED, input_text=instruction + "\n\n" + body, **kwargs)
        self.attach_pdf(record, status=Document.ExtractionStatus.READY, extracted_text=body)
        return record

    def use_client(self, **responses):
        client = FakeClient(**responses)
        patcher = patch.object(processing, "model_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def awaiting_sample(self):
        """검사를 마치고 승인 대기 중인 예시 의뢰(D3)."""
        record = self.queued_sample("analyze_D3")
        self.use_client(analyze=sample("analyze_D3")["response"])
        processing.run_analysis(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.state, Request.State.AWAITING_APPROVAL)
        return record

    def approved_sample(self):
        """본인이 승인해 외부 전송을 기다리는 예시 의뢰(D3)."""
        record = self.awaiting_sample()
        result = decide_approval(self.member, record.pk, Request.ApprovalStatus.APPROVED)
        self.assertEqual((result["state"], result["approval"]["status"]), ("queued", "approved"))
        record.refresh_from_db()
        return record

    def assert_intake_error(self, code, fn, *args, status=None):
        with self.assertRaises(IntakeError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code)
        if status is not None:
            self.assertEqual(caught.exception.status, status)


class StartProcessingTests(ProcessingTestCase):
    def test_text_request_becomes_queued_and_schedules_analysis(self):
        record = self.make_request("  가상 질문\n😀  ")
        with patch.object(processing, "launch") as launch:
            result, document = processing.start_processing(self.member, record.pk)
        record.refresh_from_db()
        self.assertEqual(result.pk, record.pk)
        self.assertIsNone(document)
        self.assertEqual(record.state, Request.State.QUEUED)
        self.assertEqual(record.input_text, "  가상 질문\n😀  ")  # 정규화하지 않는다
        self.assertIsNone(record.processing_route)
        launch.assert_called_once_with(processing.run_analysis, record.pk)

    def test_pdf_request_extracts_text_from_private_storage(self):
        record = self.make_request("요약해 주세요.")
        document = self.attach_pdf(record, text_pdf())
        with patch.object(processing, "launch"):
            processing.start_processing(self.member, record.pk)
        record.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(document.extraction_status, Document.ExtractionStatus.READY)
        self.assertIn("NCR-2026-0001", document.extracted_text)
        self.assertEqual(document.page_count, 1)
        self.assertEqual(record.input_text, "요약해 주세요." + "\n\n" + document.extracted_text)
        self.assertEqual(record.state, Request.State.QUEUED)

    def test_ready_document_text_is_reused_without_original_file(self):
        record = self.make_request("요약")
        self.attach_pdf(record, status=Document.ExtractionStatus.READY, extracted_text="이미 뽑은 본문")
        with patch.object(processing, "launch"):
            processing.start_processing(self.member, record.pk)
        record.refresh_from_db()
        self.assertEqual(record.input_text, "요약\n\n이미 뽑은 본문")

    def test_requests_that_cannot_start_are_left_unchanged(self):
        hidden = self.make_request(hidden_at=timezone.now())
        started = self.make_request(state=Request.State.QUEUED, input_text="가상 의뢰문입니다.")
        failed_doc = self.make_request()
        self.attach_pdf(failed_doc, status=Document.ExtractionStatus.FAILED)
        no_key = self.make_request()
        self.attach_pdf(no_key)
        uploading = self.make_request()
        self.attach_pdf(uploading, status=Document.ExtractionStatus.UPLOADING)
        missing_file = self.make_request()
        Document.objects.create(
            request=missing_file, original_filename="fictional.pdf", storage_object_key="requests/none/missing.pdf",
            extraction_status=Document.ExtractionStatus.PENDING,
        )
        cases = [
            (self.make_request(user=self.other), "REQUEST_NOT_FOUND", 404),
            (hidden, "REQUEST_NOT_FOUND", 404),
            (started, "PROCESSING_ALREADY_STARTED", 409),
            (self.make_request(" \n"), "INSTRUCTION_REQUIRED", 400),
            (failed_doc, "REQUEST_NOT_EDITABLE", 409),
            (no_key, "DOCUMENT_NOT_AVAILABLE", 409),
            (uploading, "DOCUMENT_NOT_READY", 409),
            (missing_file, "DOCUMENT_UNAVAILABLE", 503),
        ]
        for record, code, status in cases:
            with self.subTest(code=code):
                before = Request.objects.values().get(pk=record.pk)
                with patch.object(processing, "launch") as launch:
                    self.assert_intake_error(code, processing.start_processing, self.member, record.pk, status=status)
                launch.assert_not_called()
                self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_admin_cannot_start_processing(self):
        record = self.make_request(user=self.admin)
        with self.assertRaises(PermissionDenied):
            processing.start_processing(self.admin, record.pk)

    def test_pdf_without_text_fails_request_and_document(self):
        record = self.make_request("요약")
        document = self.attach_pdf(record, blank_pdf())
        with patch.object(processing, "launch") as launch:
            self.assert_intake_error("PDF_NO_TEXT", processing.start_processing, self.member, record.pk, status=400)
        launch.assert_not_called()
        record.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(record.state, Request.State.FAILED)
        self.assertEqual((record.error_stage, record.error_code), ("extraction", "PDF_NO_TEXT"))
        self.assertIsNotNone(record.finished_at)
        self.assertEqual(document.extraction_status, Document.ExtractionStatus.FAILED)
        self.assertIsNone(document.extracted_text)

    @override_settings(INPUT_MAX_CHARS=40)
    def test_input_over_limit_is_refused_and_extraction_rolled_back(self):
        record = self.make_request("요약")
        document = self.attach_pdf(record, text_pdf())
        self.assert_intake_error("INPUT_TOO_LONG", processing.start_processing, self.member, record.pk, status=400)
        record.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(record.state, Request.State.PREPARING)
        self.assertEqual(record.input_text, "")
        self.assertEqual(document.extraction_status, Document.ExtractionStatus.PENDING)

    def test_processing_available_follows_state_instruction_and_document(self):
        ready = self.make_request()
        pdf_only = self.make_request("")
        pending = self.attach_pdf(pdf_only)
        failed = self.make_request()
        failed_doc = self.attach_pdf(failed, status=Document.ExtractionStatus.FAILED)
        queued = self.make_request(state=Request.State.QUEUED, input_text="가상 의뢰문입니다.")
        self.assertTrue(processing.processing_available(ready, None))
        self.assertFalse(processing.processing_available(pdf_only, pending))
        self.assertFalse(processing.processing_available(failed, failed_doc))
        self.assertFalse(processing.processing_available(queued, None))


class AnalysisTests(ProcessingTestCase):
    def test_external_result_saves_spans_and_waits_for_approval(self):
        record = self.queued_sample("analyze_D3")
        response = sample("analyze_D3")["response"]
        client = self.use_client(analyze=response)
        before = timezone.now()
        processing.run_analysis(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.state, Request.State.AWAITING_APPROVAL)
        self.assertEqual(record.inspection_status, Request.InspectionStatus.COMPLETE)
        self.assertEqual(record.processing_route, Request.ProcessingRoute.EXTERNAL)
        self.assertEqual(record.approval_status, Request.ApprovalStatus.PENDING)
        self.assertEqual(record.delivery_status, Request.DeliveryStatus.NOT_SENT)
        self.assertIsNone(record.answer_text)
        self.assertGreaterEqual(record.approval_expires_at, before + timedelta(minutes=30))
        self.assertEqual(record.inspection_versions["mode"], "full")
        self.assertEqual(record.inspection_versions["llm_model"], response["versions"]["llm_model"])
        self.assertEqual(record.warning_codes, [])
        # 호출 내용: 검사 입력은 input_text 그대로, 인명·회사 가림 정책
        name, payload = client.calls[0]
        self.assertEqual(name, "analyze")
        self.assertEqual(payload["text"], record.input_text)
        self.assertTrue(payload["mask_person"] and payload["mask_company"])
        # 구간·대응표가 input_text 좌표 그대로 저장된다
        spans = list(DetectionSpan.objects.filter(request=record).select_related("sensitive_term", "mapping"))
        self.assertEqual(len(spans), len(response["spans"]))
        body_start = len(record.instruction) + 2
        for span in spans:
            self.assertEqual(record.input_text[span.start_offset:span.end_offset], span.sensitive_term.value)
            expected_source = "pdf" if span.start_offset >= body_start else "instruction"
            self.assertEqual(span.input_source, expected_source)
        placeholders = {s["placeholder"] for s in response["spans"] if s["masked"]}
        self.assertEqual(MaskingMapping.objects.filter(request=record).count(), len(placeholders))
        self.assertEqual(set(processing.mapping_dict(record)), placeholders)
        masked_spans = [s for s in spans if s.mapping_id is not None]
        self.assertEqual(len(masked_spans), sum(1 for s in response["spans"] if s["masked"]))
        # TK-63 사전 확인 조회가 그대로 읽을 수 있어야 한다
        preview = get_approval_preview(self.member, record.pk)
        self.assertEqual(preview["detection_count"], len(spans))
        self.assertEqual(preview["input_text"], record.input_text)

    def test_external_result_with_zero_spans_still_waits_for_approval(self):
        record = self.queued_sample("analyze_D4")
        self.use_client(analyze=sample("analyze_D4")["response"])
        processing.run_analysis(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.state, Request.State.AWAITING_APPROVAL)
        self.assertEqual(DetectionSpan.objects.filter(request=record).count(), 0)
        self.assertEqual(processing.build_masked_text(record), record.input_text)

    def test_degraded_result_fails_without_answering(self):
        record = self.queued_sample("analyze_D3_degraded")
        client = self.use_client(analyze=sample("analyze_D3_degraded")["response"])
        processing.run_analysis(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.state, Request.State.FAILED)
        self.assertEqual(record.inspection_status, Request.InspectionStatus.DEGRADED)
        self.assertEqual((record.error_stage, record.error_code), ("inspection", "INSPECTION_DEGRADED"))
        self.assertEqual(record.processing_route, Request.ProcessingRoute.LOCAL)  # 모델의 폴백 결정은 기록만
        self.assertIn("LLM_FAILED", record.warning_codes)
        self.assertIn("ROUTE_FAILED", record.warning_codes)
        self.assertIsNone(record.approval_status)
        self.assertGreater(DetectionSpan.objects.filter(request=record).count(), 0)  # 즉시 탐지 결과는 남긴다
        self.assertEqual([name for name, _ in client.calls], ["analyze"])

    def test_block_decision_blocks_request(self):
        record = self.queued_sample()
        response = sample("analyze_D3")["response"]
        response["decision"] = {"action": "block", "needs_approval": False, "reasons": ["가상 차단"], "warnings": []}
        self.use_client(analyze=response)
        processing.run_analysis(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.state, Request.State.BLOCKED)
        self.assertEqual(record.processing_route, Request.ProcessingRoute.BLOCKED)
        self.assertIsNotNone(record.finished_at)
        self.assertIsNone(record.approval_status)

    def test_local_decision_generates_internal_answer(self):
        record = self.queued_sample()
        response = sample("analyze_D3")["response"]
        response["decision"] = {"action": "local", "needs_approval": False, "reasons": [], "warnings": []}
        local = sample("answer_local_D2")["response"]
        client = self.use_client(analyze=response, answer_local=local)
        processing.run_analysis(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.state, Request.State.COMPLETED)
        self.assertEqual(record.processing_route, Request.ProcessingRoute.LOCAL)
        self.assertEqual(record.answer_text, local["answer"])
        self.assertIsNone(record.delivery_status)
        self.assertIsNone(record.approval_status)
        self.assertEqual(client.calls[1][0], "answer_local")
        self.assertEqual(client.calls[1][1]["text"], record.input_text)

    def test_local_answer_problems_fail_the_request(self):
        response = sample("analyze_D3")["response"]
        response["decision"] = {"action": "local", "needs_approval": False, "reasons": [], "warnings": []}
        local = sample("answer_local_D2")["response"]
        cases = [
            ("ANSWER_TRUNCATED", dict(local, truncated=True)),
            ("ANSWER_EMPTY", dict(local, answer=" \n")),
            ("MODEL_UNAVAILABLE", Unavailable(None, "가상 연결 실패", "/answer_local")),
        ]
        for code, answer in cases:
            with self.subTest(code=code):
                record = self.queued_sample()
                self.use_client(analyze=response, answer_local=answer)
                processing.run_analysis(record.pk)
                record.refresh_from_db()
                self.assertEqual(record.state, Request.State.FAILED)
                self.assertEqual((record.error_stage, record.error_code), ("local_answer", code))
                self.assertIsNone(record.answer_text)

    def test_analyze_failure_marks_inspection_failed(self):
        record = self.queued_sample()
        self.use_client(analyze=Unavailable(None, "가상 연결 실패", "/analyze"))
        processing.run_analysis(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.state, Request.State.FAILED)
        self.assertEqual(record.inspection_status, Request.InspectionStatus.FAILED)
        self.assertEqual((record.error_stage, record.error_code), ("inspection", "MODEL_UNAVAILABLE"))
        self.assertEqual(DetectionSpan.objects.filter(request=record).count(), 0)

    def test_unknown_decision_fails(self):
        record = self.queued_sample("analyze_D4")
        response = sample("analyze_D4")["response"]
        response["decision"] = {"action": "mystery", "warnings": []}
        self.use_client(analyze=response)
        processing.run_analysis(record.pk)
        record.refresh_from_db()
        self.assertEqual((record.state, record.error_code), (Request.State.FAILED, "DECISION_UNKNOWN"))

    def test_unmasked_span_with_unsupported_label_is_dropped_with_warning_code(self):
        record = self.queued_sample("analyze_D4")
        response = sample("analyze_D4")["response"]
        response["spans"] = [
            {"start": 0, "end": 2, "text": record.input_text[:2], "label": "other", "masked": False, "placeholder": None},
        ]
        response["route"]["truncated"] = True
        self.use_client(analyze=response)
        processing.run_analysis(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.state, Request.State.AWAITING_APPROVAL)  # 가려지지 않는 구간이라 전송문은 그대로다
        self.assertEqual(DetectionSpan.objects.filter(request=record).count(), 0)
        for code in ("SPAN_LABEL_UNSUPPORTED", "ROUTE_INPUT_TRUNCATED"):
            self.assertIn(code, record.warning_codes)
        self.assertEqual(SensitiveTerm.objects.filter(label="other").count(), 0)

    def test_span_that_cannot_be_saved_fails_instead_of_continuing(self):
        """저장하지 못한 구간(여기서는 겹침)의 원문은 다시 만든 전송문에 그대로 남는다. 불완전 검사처럼 실패로 끝낸다."""
        routes = {"external": Request.ProcessingRoute.EXTERNAL, "local": Request.ProcessingRoute.LOCAL}
        for action, route in routes.items():
            with self.subTest(action=action):
                record = self.queued_sample("analyze_D4")
                text = record.input_text
                first = text.index("AS9100D")
                response = sample("analyze_D4")["response"]
                response["decision"] = {"action": action, "needs_approval": action == "external", "reasons": [], "warnings": []}
                response["spans"] = [
                    {"start": first, "end": first + 7, "text": "AS9100D", "label": "doc_no", "masked": True,
                     "placeholder": "[[ID_1]]"},
                    {"start": first + 2, "end": first + 9, "text": text[first + 2:first + 9], "label": "project", "masked": True,
                     "placeholder": "[[PROJECT_1]]"},  # 앞 구간과 겹친다
                ]
                client = self.use_client(analyze=response)
                processing.run_analysis(record.pk)
                record.refresh_from_db()
                self.assertEqual(record.state, Request.State.FAILED)
                self.assertEqual((record.error_stage, record.error_code), ("inspection", "SPAN_SAVE_SKIPPED"))
                self.assertIn("SPAN_SAVE_SKIPPED", record.warning_codes)
                self.assertEqual(record.processing_route, route)
                self.assertIsNone(record.approval_status)
                self.assertIsNotNone(record.finished_at)
                self.assertEqual(DetectionSpan.objects.filter(request=record).count(), 1)  # 저장된 구간은 남긴다
                self.assertEqual([name for name, _ in client.calls], ["analyze"])  # 사내 답변도 만들지 않는다

    def test_request_with_a_failed_span_save_cannot_be_approved_or_sent(self):
        """가려져야 할 구간 하나가 저장에 실패하면, 그 값이 전송문에 원문으로 남고 대응표에 없어 유출 검사도 잡지 못한다."""
        response = sample("analyze_D3")["response"]
        target = next(span for span in response["spans"] if span["masked"])
        record = self.queued_sample("analyze_D3")
        client = self.use_client(analyze=response, answer_external=sample("answer_external_D3")["response"])
        create_span = DetectionSpan.objects.create

        def failing_create(**fields):
            if fields["start_offset"] == target["start"]:
                raise IntegrityError("가상 저장 실패")
            return create_span(**fields)

        with patch.object(DetectionSpan.objects, "create", side_effect=failing_create):
            processing.run_analysis(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.state, Request.State.FAILED)
        self.assertEqual((record.error_stage, record.error_code), ("inspection", "SPAN_SAVE_SKIPPED"))
        self.assertIsNone(record.approval_status)
        self.assertEqual(DetectionSpan.objects.filter(request=record).count(), len(response["spans"]) - 1)
        self.assertIn(target["text"], processing.build_masked_text(record))  # 이대로 보냈다면 원문이 나갔을 전송문
        self.assert_intake_error("APPROVAL_NOT_AVAILABLE", decide_approval, self.member, record.pk, "approved")
        self.assertIsNone(processing.run_external(record.pk))
        self.assertEqual([name for name, _ in client.calls], ["analyze"])  # 외부 전송 호출이 없다

    def test_result_that_does_not_rebuild_to_the_model_mask_fails_before_approval(self):
        """DB 로 다시 만든 전송문·대응표가 모델 서비스가 가린 결과와 다르면, 가려지지 않은 원문이 전송문에 남은 것이다."""
        def masked_span_with_unknown_label(text, response):
            start = text.index("AS9100D")
            response["spans"] = [
                {"start": start, "end": start + 7, "text": "AS9100D", "label": "other", "masked": True, "placeholder": "[[X_1]]"},
            ]
            response["mask"]["masked"] = text[:start] + "[[X_1]]" + text[start + 7:]
            response["mask"]["mapping"] = {"[[X_1]]": "AS9100D"}

        cases = {
            "가려진 구간의 라벨이 DB 에 없어 버려진다": masked_span_with_unknown_label,
            "대응표가 다르다": lambda text, response: response["mask"]["mapping"].update({"[[ID_9]]": "가상 값"}),
            "응답에 mask 가 없다": lambda text, response: response.pop("mask"),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                record = self.queued_sample("analyze_D4")
                response = sample("analyze_D4")["response"]
                change(record.input_text, response)
                self.use_client(analyze=response)
                processing.run_analysis(record.pk)
                record.refresh_from_db()
                self.assertEqual(record.state, Request.State.FAILED)
                self.assertEqual((record.error_stage, record.error_code), ("inspection", "MASK_MISMATCH"))
                self.assertEqual(record.processing_route, Request.ProcessingRoute.EXTERNAL)
                self.assertIsNone(record.approval_status)
                self.assertIsNotNone(record.finished_at)

    def test_only_queued_requests_without_decision_are_analyzed(self):
        preparing = self.make_request()
        awaiting = self.make_request(
            state=Request.State.AWAITING_APPROVAL, input_text="가상", inspection_status="complete",
            processing_route="external", approval_status="pending", approval_expires_at=timezone.now(),
        )
        approved = self.make_request(
            state=Request.State.QUEUED, input_text="가상", inspection_status="complete", processing_route="external",
            approval_status="approved", decided_at=timezone.now(), approval_expires_at=timezone.now(),
        )
        client = self.use_client(analyze=sample("analyze_D4")["response"])
        for record in (preparing, awaiting, approved):
            with self.subTest(state=record.state):
                before = Request.objects.values().get(pk=record.pk)
                self.assertIsNone(processing.run_analysis(record.pk))
                self.assertEqual(Request.objects.values().get(pk=record.pk), before)
        self.assertEqual(client.calls, [])


class ExternalSendTests(ProcessingTestCase):
    def test_approved_request_is_sent_masked_and_completed(self):
        record = self.approved_sample()
        external = sample("answer_external_D3")["response"]
        client = self.use_client(answer_external=external)
        before = timezone.now()
        processing.run_external(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.state, Request.State.COMPLETED)
        self.assertEqual(record.delivery_status, Request.DeliveryStatus.CONFIRMED_SENT)
        self.assertGreaterEqual(record.external_send_started_at, before)
        self.assertEqual(record.answer_text, external["restored"])
        self.assertIsNotNone(record.finished_at)
        name, payload = client.calls[-1]
        self.assertEqual(name, "answer_external")
        mapping = processing.mapping_dict(record)
        self.assertEqual(payload["mapping"], mapping)
        self.assertEqual(payload["masked"], processing.build_masked_text(record))
        # 전송문에는 가린 원문 값이 없고 자리표만 있다
        for placeholder, value in mapping.items():
            self.assertIn(placeholder, payload["masked"])
            self.assertNotIn(value, payload["masked"])
        self.assertNotEqual(payload["masked"], record.input_text)

    def test_send_problems_fail_the_request_with_delivery_status(self):
        external = sample("answer_external_D3")["response"]
        cases = [
            ("external_send", "MODEL_UNAVAILABLE", Unavailable(None, "가상 시간 초과", "/answer_external"), "unknown"),
            ("external_send", "LEAK_BLOCKED", LeakBlocked(409, "가상 유출 검사", "/answer_external"), "not_sent"),
            ("external_send", "EXTERNAL_REFUSAL", dict(external, refusal="가상 거부", stop_reason="refusal"), "confirmed_sent"),
            ("restore", "RESTORE_INCOMPLETE", dict(external, unresolved=["[[ORG_9]]"]), "confirmed_sent"),
            ("external_send", "ANSWER_TRUNCATED", dict(external, truncated=True), "confirmed_sent"),
            ("external_send", "ANSWER_EMPTY", dict(external, restored=""), "confirmed_sent"),
        ]
        for stage, code, answer, delivery in cases:
            with self.subTest(code=code):
                record = self.approved_sample()
                self.use_client(answer_external=answer)
                processing.run_external(record.pk)
                record.refresh_from_db()
                self.assertEqual(record.state, Request.State.FAILED)
                self.assertEqual((record.error_stage, record.error_code), (stage, code))
                self.assertEqual(record.delivery_status, delivery)
                self.assertIsNone(record.answer_text)
                self.assertIsNotNone(record.external_send_started_at)

    def test_unapproved_rejected_or_already_started_requests_are_not_sent(self):
        awaiting = self.awaiting_sample()
        rejected = self.awaiting_sample()
        decide_approval(self.member, rejected.pk, Request.ApprovalStatus.REJECTED)
        started = self.approved_sample()
        Request.objects.filter(pk=started.pk).update(external_send_started_at=timezone.now())
        local = self.make_request(
            state=Request.State.QUEUED, input_text="가상", inspection_status="complete", processing_route="local",
            approval_status="approved", decided_at=timezone.now(),
        )
        client = self.use_client(answer_external=sample("answer_external_D3")["response"])
        for record in (awaiting, rejected, started, local):
            with self.subTest(state=Request.objects.get(pk=record.pk).state):
                before = Request.objects.values().get(pk=record.pk)
                self.assertIsNone(processing.run_external(record.pk))
                self.assertEqual(Request.objects.values().get(pk=record.pk), before)
        self.assertEqual(client.calls, [])

    def test_approved_request_with_an_unsaved_span_is_not_sent(self):
        """수정 전 코드가 승인 대기로 보낸 의뢰가 남아 있어도, 저장하지 못한 구간이 있으면 보내지 않는다."""
        record = self.approved_sample()
        Request.objects.filter(pk=record.pk).update(warning_codes=["SPAN_SAVE_SKIPPED"])
        client = self.use_client(answer_external=sample("answer_external_D3")["response"])
        processing.run_external(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.state, Request.State.FAILED)
        self.assertEqual((record.error_stage, record.error_code), ("external_send", "SPAN_SAVE_SKIPPED"))
        self.assertEqual(record.delivery_status, Request.DeliveryStatus.NOT_SENT)
        self.assertIsNone(record.answer_text)
        self.assertEqual(client.calls, [])


class ConcurrentChangeTests(ProcessingTestCase):
    """모델 호출 도중에 다른 쪽이 의뢰를 바꾼 경우. 가짜 클라이언트가 응답을 돌려주기 직전에 그 변경을 실행한다(FakeClient.during).
    SQLite 에는 행 잠금이 없어, 여기서는 순서를 정해 둔 끼어들기로 결과 저장의 판단만 검증한다."""

    STUCK = (Request.State.FAILED, "system", "STUCK")

    def local_response(self):
        response = sample("analyze_D3")["response"]
        response["decision"] = {"action": "local", "needs_approval": False, "reasons": [], "warnings": []}
        return response

    def hide(self, record):
        return lambda: hide_request(self.member, record.pk)

    def stuck_cleanup(self, record):
        """장시간 진행 정리가 이 의뢰를 실패(STUCK)로 돌리는 끼어들기."""
        def run():
            Request.objects.filter(pk=record.pk).update(updated_at=timezone.now() - timedelta(minutes=16))
            self.assertEqual(processing.fail_stuck(), 1)
        return run

    def outcome(self, record):
        record.refresh_from_db()
        return record.state, record.error_stage, record.error_code

    def test_hiding_during_a_model_call_is_not_undone_by_the_result(self):
        State = Request.State
        analyzed, local = sample("analyze_D3")["response"], sample("answer_local_D2")["response"]
        external = sample("answer_external_D3")["response"]
        cases = [
            ("analyze", self.queued_sample, dict(analyze=analyzed), State.AWAITING_APPROVAL),
            ("analyze", self.queued_sample, dict(analyze=Unavailable(None, "가상 연결 실패", "/analyze")), State.FAILED),
            ("answer_local", self.queued_sample, dict(analyze=self.local_response(), answer_local=local), State.COMPLETED),
            ("answer_external", self.approved_sample, dict(answer_external=external), State.COMPLETED),
            ("answer_external", self.approved_sample,
             dict(answer_external=LeakBlocked(409, "가상 유출 검사", "/answer_external")), State.FAILED),
        ]
        for call, make, responses, state in cases:
            with self.subTest(call=call, state=state):
                record = make()
                self.use_client(during={call: self.hide(record)}, **responses)
                worker = processing.run_external if call == "answer_external" else processing.run_analysis
                worker(record.pk)
                record.refresh_from_db()
                self.assertEqual(record.state, state)
                self.assertIsNotNone(record.hidden_at)

    def test_late_analysis_result_does_not_revive_a_request_failed_meanwhile(self):
        """장시간 진행 정리가 실패로 돌린 뒤에 도착한 검사 결과(응답이든 오류든)는 버린다. 실패 기록이 그대로 남는다."""
        results = {"응답": sample("analyze_D3")["response"], "오류": Unavailable(None, "가상 시간 초과", "/analyze")}
        for name, result in results.items():
            with self.subTest(result=name):
                record = self.queued_sample("analyze_D3")
                self.use_client(during={"analyze": self.stuck_cleanup(record)}, analyze=result)
                self.assertIsNone(processing.run_analysis(record.pk))
                self.assertEqual(self.outcome(record), self.STUCK)
                self.assertIsNotNone(record.finished_at)
                self.assertEqual(record.inspection_status, Request.InspectionStatus.PENDING)
                self.assertIsNone(record.approval_status)
                self.assertEqual(DetectionSpan.objects.filter(request=record).count(), 0)

    def test_late_local_answer_does_not_complete_a_request_failed_meanwhile(self):
        record = self.queued_sample("analyze_D3")
        self.use_client(
            during={"answer_local": self.stuck_cleanup(record)}, analyze=self.local_response(),
            answer_local=sample("answer_local_D2")["response"],
        )
        self.assertIsNone(processing.run_analysis(record.pk))
        self.assertEqual(self.outcome(record), self.STUCK)
        self.assertIsNone(record.answer_text)

    def test_late_external_result_keeps_the_failure_and_records_whether_it_was_sent(self):
        """전송 응답을 기다리는 사이 실패로 정리된 의뢰는 실패로 두되, 외부로 나갔는지는 사실대로 남긴다."""
        Delivery = Request.DeliveryStatus
        cases = [
            ("응답 도착", sample("answer_external_D3")["response"], Delivery.CONFIRMED_SENT),
            ("유출 검사로 거부", LeakBlocked(409, "가상 유출 검사", "/answer_external"), Delivery.NOT_SENT),
            ("응답 유실", Unavailable(None, "가상 시간 초과", "/answer_external"), Delivery.UNKNOWN),
        ]
        for name, result, delivery in cases:
            with self.subTest(result=name):
                record = self.approved_sample()
                self.use_client(during={"answer_external": self.stuck_cleanup(record)}, answer_external=result)
                self.assertIsNone(processing.run_external(record.pk))
                self.assertEqual(self.outcome(record), self.STUCK)
                self.assertEqual(record.delivery_status, delivery)
                self.assertIsNone(record.answer_text)


class MaintenanceTests(ProcessingTestCase):
    def test_dispatch_runs_queued_analysis_and_approved_sends(self):
        analysis = self.queued_sample("analyze_D4")
        approved = self.queued_sample("analyze_D3")
        client = self.use_client(analyze=sample("analyze_D3")["response"], answer_external=sample("answer_external_D3")["response"])
        processing.run_analysis(approved.pk)
        decide_approval(self.member, approved.pk, Request.ApprovalStatus.APPROVED)
        client.responses["analyze"] = sample("analyze_D4")["response"]
        self.assertEqual(processing.dispatch_pending(), 2)
        self.assertEqual(Request.objects.get(pk=analysis.pk).state, Request.State.AWAITING_APPROVAL)
        self.assertEqual(Request.objects.get(pk=approved.pk).state, Request.State.COMPLETED)
        self.assertEqual(processing.dispatch_pending(), 0)

    def test_stuck_requests_are_failed_after_timeout(self):
        stuck = self.make_request(state=Request.State.ANALYZING, input_text="가상")
        recent = self.make_request(state=Request.State.PROCESSING, input_text="가상", processing_route="local")
        Request.objects.filter(pk=stuck.pk).update(updated_at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(processing.fail_stuck(), 1)
        stuck.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual((stuck.state, stuck.error_code), (Request.State.FAILED, "STUCK"))
        self.assertEqual(recent.state, Request.State.PROCESSING)

    def test_stuck_cleanup_skips_a_request_that_finished_before_it_was_locked(self):
        """정리 대상을 고른 뒤 잠그기 전에 작업자가 끝낸 의뢰는 건드리지 않는다(답변을 지우지 않는다)."""
        record = self.make_request(state=Request.State.PROCESSING, input_text="가상", processing_route="local")
        Request.objects.filter(pk=record.pk).update(updated_at=timezone.now() - timedelta(minutes=16))
        read_locked = processing.locked

        def finish_first(request_id, **filters):
            Request.objects.filter(pk=request_id).update(
                state=Request.State.COMPLETED, answer_text="가상 답변", updated_at=timezone.now(),
            )
            return read_locked(request_id, **filters)

        with patch.object(processing, "locked", side_effect=finish_first):
            self.assertEqual(processing.fail_stuck(), 0)
        record.refresh_from_db()
        self.assertEqual((record.state, record.answer_text, record.error_code), (Request.State.COMPLETED, "가상 답변", None))

    def test_process_requests_command_reports_counts(self):
        record = self.queued_sample("analyze_D4")
        expired = self.make_request(
            state=Request.State.AWAITING_APPROVAL, input_text="가상", inspection_status="complete",
            processing_route="external", approval_status="pending", delivery_status="not_sent",
            approval_expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.use_client(analyze=sample("analyze_D4")["response"])
        out = StringIO()
        call_command("process_requests", stdout=out)
        self.assertIn("처리 1건, 기한 만료 1건", out.getvalue())
        self.assertEqual(Request.objects.get(pk=record.pk).state, Request.State.AWAITING_APPROVAL)
        self.assertEqual(Request.objects.get(pk=expired.pk).state, Request.State.EXPIRED)

    def crashing_then_healthy(self):
        """예상 밖 형식의 응답을 받을 의뢰와 정상 의뢰. 앞의 것이 먼저 처리된다."""
        crashing, healthy = self.queued_sample("analyze_D3"), self.queued_sample("analyze_D3")
        Request.objects.filter(pk=crashing.pk).update(updated_at=timezone.now() - timedelta(minutes=1))
        malformed = sample("analyze_D3")["response"]
        del malformed["spans"][0]["start"]
        self.use_client(analyze=[malformed, sample("analyze_D3")["response"]])
        return crashing, healthy

    def assert_crash_recorded_and_next_processed(self, crashing, healthy):
        crashing.refresh_from_db()
        healthy.refresh_from_db()
        self.assertEqual(
            (crashing.state, crashing.error_stage, crashing.error_code), (Request.State.FAILED, "system", "INTERNAL_ERROR"),
        )
        self.assertEqual(healthy.state, Request.State.AWAITING_APPROVAL)

    def test_command_records_a_crashing_request_and_processes_the_next(self):
        crashing, healthy = self.crashing_then_healthy()
        out = StringIO()
        with self.assertLogs("gateway.processing", level="ERROR"):
            call_command("process_requests", stdout=out)
        self.assertIn("처리 1건", out.getvalue())
        self.assert_crash_recorded_and_next_processed(crashing, healthy)

    def test_looping_command_survives_a_crashing_request(self):
        crashing, healthy = self.crashing_then_healthy()

        class StopLoop(Exception):
            """반복을 끝내려고 주기 끝의 대기에서 올린다."""

        with patch("gateway.management.commands.process_requests.time.sleep", side_effect=StopLoop):
            with self.assertLogs("gateway.processing", level="ERROR"), self.assertRaises(StopLoop):
                call_command("process_requests", loop=True, stdout=StringIO())
        self.assert_crash_recorded_and_next_processed(crashing, healthy)

    @override_settings(GATEWAY_WORKER="thread")
    def test_launch_starts_worker_thread_after_commit(self):
        record = self.make_request(state=Request.State.QUEUED, input_text="가상")
        with patch.object(processing.threading, "Thread") as thread:
            with self.captureOnCommitCallbacks(execute=True):
                processing.launch(processing.run_analysis, record.pk)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["target"], processing.run_guarded)
        self.assertEqual(thread.call_args.kwargs["args"], (processing.run_analysis, record.pk))
        thread.return_value.start.assert_called_once()

    def test_launch_is_disabled_in_command_mode(self):
        with patch.object(processing.threading, "Thread") as thread:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                processing.launch(processing.run_analysis, uuid4())
        self.assertEqual(callbacks, [])
        thread.assert_not_called()

    def test_unexpected_worker_crash_is_recorded_as_failure(self):
        record = self.make_request(state=Request.State.QUEUED, input_text="가상")

        def broken(request_id):
            raise RuntimeError("가상 예외")

        with patch.object(processing.connections, "close_all"):
            processing.run_guarded(broken, record.pk)
        record.refresh_from_db()
        self.assertEqual((record.state, record.error_stage, record.error_code), (Request.State.FAILED, "system", "INTERNAL_ERROR"))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ResultWriteLockTests(TransactionTestCase):
    """PostgreSQL 의 행 잠금으로 작업자의 결과 저장과 숨김·장시간 진행 정리가 겹칠 때를 검증한다. 실제 AI 호출은 없다.
    한쪽이 행을 잠근 채 멈춰 있게 하고, 다른 쪽이 그 잠금을 기다리기 시작한 것을 확인한 뒤 풀어 준다."""

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL의 행 잠금으로 검증합니다.")
        self.user = get_user_model().objects.create_user(
            username="USR70901", full_name="결과 저장 잠금 검증용 가상 사용자", password="SecurePass1!",
        )
        self.user.groups.add(Group.objects.create(name=USER_GROUP_NAME))
        self.record = Request.objects.create(
            user=self.user, client_request_key=uuid4(), instruction="가상 의뢰", input_text="가상 의뢰",
            state=Request.State.PROCESSING, inspection_status="complete", processing_route="local",
        )
        self.holding, self.release = Event(), Event()

    def in_thread(self, action):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
            return action()
        finally:
            connections.close_all()

    def pause(self):
        """행을 잠근 채 멈춘다. overlap 이 상대의 잠금 대기를 확인하고 풀어 준다."""
        self.holding.set()
        if not self.release.wait(timeout=20):
            raise AssertionError("잠금을 풀라는 신호가 오지 않았습니다.")

    def wait_for_lock_waiter(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.05)
        self.fail("행 잠금을 기다리는 연결이 생기지 않았습니다.")

    def overlap(self, holder, waiter):
        """holder 가 행을 잠그고 멈춘 동안 waiter 를 시작하고, waiter 가 잠금을 기다리는 것을 확인한 뒤 holder 를 끝낸다."""
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                first = pool.submit(self.in_thread, holder)
                self.assertTrue(self.holding.wait(timeout=10), "먼저 시작한 쪽이 행을 잠그지 못했습니다.")
                second = pool.submit(self.in_thread, waiter)
                self.wait_for_lock_waiter()
            finally:
                self.release.set()
            return first.result(timeout=20), second.result(timeout=20)

    def write_answer(self, paused=False):
        """작업자의 결과 저장. 썼으면 True, 늦은 결과로 버려졌으면 False."""
        def change(fresh):
            if paused:
                self.pause()
            processing.complete(fresh, "가상 최종 답변", False, "local_answer")
        return processing.apply_result(self.record.pk, Request.State.PROCESSING, change) is not None

    def make_stuck(self):
        Request.objects.filter(pk=self.record.pk).update(updated_at=timezone.now() - timedelta(minutes=16))

    def outcome(self):
        self.record.refresh_from_db()
        return self.record.state, self.record.error_code, self.record.answer_text

    def test_hide_waits_for_the_result_write_and_both_are_kept(self):
        def hide():
            return hide_request(get_user_model().objects.get(pk=self.user.pk), self.record.pk)

        written, hidden_at = self.overlap(lambda: self.write_answer(paused=True), hide)
        self.assertTrue(written)
        self.assertEqual(self.outcome(), (Request.State.COMPLETED, None, "가상 최종 답변"))
        self.assertIsNotNone(hidden_at)
        self.assertEqual(self.record.hidden_at, hidden_at)

    def test_stuck_cleanup_waits_for_the_result_write_and_leaves_it(self):
        self.make_stuck()
        written, failed = self.overlap(lambda: self.write_answer(paused=True), processing.fail_stuck)
        self.assertEqual((written, failed), (True, 0))
        self.assertEqual(self.outcome(), (Request.State.COMPLETED, None, "가상 최종 답변"))

    def test_late_result_write_waits_for_stuck_cleanup_and_is_discarded(self):
        self.make_stuck()
        fail = processing.fail

        def paused_fail(*args, **kwargs):
            self.pause()
            return fail(*args, **kwargs)

        with patch.object(processing, "fail", side_effect=paused_fail):
            failed, written = self.overlap(processing.fail_stuck, self.write_answer)
        self.assertEqual((failed, written), (1, False))
        self.assertEqual(self.outcome(), (Request.State.FAILED, "STUCK", None))
