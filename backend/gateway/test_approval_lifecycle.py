"""가상 검사 결과로 승인 대기 준비와 만료 명령을 검증한다. 실제 AI는 호출하지 않는다."""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from .approval_lifecycle import expire_due_approvals, prepare_approval
from .intake_validation import IntakeError
from .models import DetectionSpan, Document, Request, SensitiveTerm


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ApprovalLifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="USR63401", full_name="기한 검증용 가상 사용자", password="SecurePass1!",
        )

    def make_request(self, **kwargs):
        data = {
            "user": self.user, "client_request_key": uuid4(), "instruction": "가상 의뢰",
            "input_text": "가상 의뢰", "state": "analyzing",
            "inspection_status": "complete", "processing_route": "external",
        }
        data.update(kwargs)
        return Request.objects.create(**data)

    def pending(self, deadline, **kwargs):
        data = {"state": "awaiting_approval", "approval_status": "pending", "approval_expires_at": deadline}
        data.update(kwargs)
        return self.make_request(**data)

    def assert_intake_error(self, code, fn, *args):
        with self.assertRaises(IntakeError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code)

    def test_prepares_text_request_with_zero_detections_and_exactly_thirty_minutes(self):
        record = self.make_request()
        now = timezone.now()
        with patch("gateway.approval_lifecycle.timezone.now", return_value=now):
            result, created = prepare_approval(record.pk)
        self.assertTrue(created)
        record.refresh_from_db()
        self.assertEqual(result.pk, record.pk)
        self.assertEqual(record.state, "awaiting_approval")
        self.assertEqual(record.approval_status, "pending")
        self.assertEqual(record.approval_expires_at, now + timedelta(minutes=30))
        self.assertEqual(record.input_text, "가상 의뢰")
        self.assertIsNone(record.decided_at)
        self.assertIsNone(record.external_send_started_at)
        self.assertIsNone(record.finished_at)

    def test_ready_pdf_and_saved_spans_are_preserved_without_recombining_text(self):
        text = "가상 의뢰\r\n\r\n😀 가상기업 "
        record = self.make_request(input_text=text)
        Document.objects.create(request=record, original_filename="가상.pdf", extracted_text="😀 가상기업 ", extraction_status="ready")
        term = SensitiveTerm.objects.create(value="가상기업", label="company")
        span = DetectionSpan.objects.create(
            request=record, sensitive_term=term, input_source="pdf",
            start_offset=text.index("가상기업"), end_offset=text.index("가상기업") + 4,
        )
        before = DetectionSpan.objects.values().get(pk=span.pk)
        _, created = prepare_approval(record.pk)
        self.assertTrue(created)
        record.refresh_from_db()
        self.assertEqual(record.input_text, text)
        self.assertEqual(DetectionSpan.objects.values().get(pk=span.pk), before)

    def test_repeated_preparation_does_not_extend_deadline_or_update_timestamp(self):
        record = self.make_request()
        result, _ = prepare_approval(record.pk)
        before = Request.objects.values().get(pk=record.pk)
        with patch("gateway.approval_lifecycle.timezone.now", return_value=result.approval_expires_at - timedelta(minutes=1)):
            _, created = prepare_approval(record.pk)
        self.assertFalse(created)
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_overdue_preparation_does_not_restart_waiting_period(self):
        record = self.pending(timezone.now() - timedelta(seconds=1))
        before = Request.objects.values().get(pk=record.pk)
        self.assert_intake_error("APPROVAL_EXPIRED", prepare_approval, record.pk)
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_non_eligible_states_and_results_cannot_start_approval(self):
        cases = [{"state": state} for state in Request.State.values if state not in ("analyzing", "awaiting_approval")]
        cases += [{"inspection_status": status} for status in ("pending", "degraded", "failed")]
        cases += [{"processing_route": route} for route in (None, "local", "blocked")]
        cases += [{"approval_status": status} for status in Request.ApprovalStatus.values]
        cases += [{field: timezone.now()} for field in ("approval_expires_at", "decided_at", "finished_at", "external_send_started_at")]
        cases += [{"delivery_status": status} for status in ("unknown", "confirmed_sent")]
        for fields in cases:
            with self.subTest(fields=fields):
                record = self.make_request(**fields)
                before = Request.objects.values().get(pk=record.pk)
                self.assert_intake_error("APPROVAL_NOT_AVAILABLE", prepare_approval, record.pk)
                self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_pdf_must_have_completed_nonempty_extraction(self):
        cases = [(status, "가상 본문") for status in Document.ExtractionStatus.values if status != "ready"]
        cases += [("ready", text) for text in (None, "", " \n")]
        for status, text in cases:
            with self.subTest(status=status, text=text):
                record = self.make_request()
                Document.objects.create(request=record, original_filename="가상.pdf", extraction_status=status, extracted_text=text)
                self.assert_intake_error("PREVIEW_NOT_AVAILABLE", prepare_approval, record.pk)
                record.refresh_from_db()
                self.assertEqual(record.state, "analyzing")

    def test_missing_or_oversized_input_cannot_start_approval(self):
        cases = [{"instruction": text} for text in ("", " \n", "가" * (settings.INSTRUCTION_MAX_CHARS + 1))]
        cases += [{"input_text": text} for text in ("", " \n", "가" * (settings.INPUT_MAX_CHARS + 1))]
        for fields in cases:
            with self.subTest(field=next(iter(fields))):
                record = self.make_request(**fields)
                self.assert_intake_error("PREVIEW_NOT_AVAILABLE", prepare_approval, record.pk)
                record.refresh_from_db()
                self.assertIsNone(record.approval_expires_at)

    def test_input_at_configured_limit_is_accepted(self):
        record = self.make_request(instruction="가" * settings.INSTRUCTION_MAX_CHARS, input_text="가" * settings.INPUT_MAX_CHARS)
        self.assertTrue(prepare_approval(record.pk)[1])

    def test_corrupt_span_prevents_transition(self):
        record = self.make_request()
        term = SensitiveTerm.objects.create(value="가상", label="company")
        span = DetectionSpan.objects.create(request=record, sensitive_term=term, input_source="instruction", start_offset=0, end_offset=2)
        DetectionSpan.objects.filter(pk=span.pk).update(input_source="invalid")
        self.assert_intake_error("PREVIEW_NOT_AVAILABLE", prepare_approval, record.pk)
        record.refresh_from_db()
        self.assertIsNone(record.approval_expires_at)

    def test_missing_request_returns_not_found(self):
        self.assert_intake_error("REQUEST_NOT_FOUND", prepare_approval, uuid4())

    def test_outer_result_transaction_can_roll_back_preparation(self):
        record = self.make_request(inspection_status="pending")
        before = Request.objects.values().get(pk=record.pk)
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                Request.objects.select_for_update().get(pk=record.pk)
                Request.objects.filter(pk=record.pk).update(inspection_status="complete")
                prepare_approval(record.pk)
                raise DatabaseError("가상 저장 실패")
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_expiry_boundary_and_hidden_requests(self):
        now = timezone.now()
        past = self.pending(now - timedelta(seconds=1), hidden_at=now)
        boundary = self.pending(now)
        future = self.pending(now + timedelta(seconds=1))
        with patch("gateway.approval_lifecycle.timezone.now", return_value=now):
            self.assertEqual(expire_due_approvals(), 2)
        for record in (past, boundary):
            record.refresh_from_db()
            self.assertEqual(record.state, "expired")
            self.assertEqual(record.approval_status, "expired")
            self.assertEqual(record.finished_at, record.approval_expires_at)
            self.assertIsNone(record.decided_at)
        future.refresh_from_db()
        self.assertEqual(future.state, "awaiting_approval")

    def test_decided_sent_or_other_state_records_are_not_overwritten(self):
        deadline = timezone.now() - timedelta(minutes=1)
        cases = [{"state": state} for state in Request.State.values if state != "awaiting_approval"]
        cases += [{"approval_status": status} for status in (None, "approved", "rejected", "expired")]
        cases += [{field: timezone.now()} for field in ("decided_at", "finished_at", "external_send_started_at")]
        cases += [{"delivery_status": status} for status in ("unknown", "confirmed_sent")]
        cases += [{"approval_expires_at": None}]
        for fields in cases:
            self.pending(deadline, **fields)
        before = list(Request.objects.order_by("pk").values())
        self.assertEqual(expire_due_approvals(), 0)
        self.assertEqual(list(Request.objects.order_by("pk").values()), before)

    def test_small_batches_process_all_due_records_and_rerun_is_noop(self):
        for _ in range(5):
            self.pending(timezone.now() - timedelta(seconds=1))
        self.assertEqual(expire_due_approvals(batch_size=2), 5)
        before = list(Request.objects.order_by("pk").values())
        self.assertEqual(expire_due_approvals(batch_size=2), 0)
        self.assertEqual(list(Request.objects.order_by("pk").values()), before)

    def test_command_dry_run_reports_count_without_sensitive_output_or_changes(self):
        record = self.pending(timezone.now() - timedelta(seconds=1), input_text="가상 비밀 원문")
        before = Request.objects.values().get(pk=record.pk)
        output = StringIO()
        call_command("expire_approvals", "--dry-run", stdout=output)
        self.assertIn("만료 대상: 1건 (변경 없음)", output.getvalue())
        self.assertNotIn("가상 비밀 원문", output.getvalue())
        self.assertNotIn(str(record.pk), output.getvalue())
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_command_applies_expiry_and_can_be_repeated(self):
        self.pending(timezone.now() - timedelta(seconds=1))
        output = StringIO()
        call_command("expire_approvals", "--batch-size", "1", stdout=output)
        self.assertIn("만료 처리 완료: 1건", output.getvalue())
        output = StringIO()
        call_command("expire_approvals", stdout=output)
        self.assertIn("만료 처리 완료: 0건", output.getvalue())

    def test_invalid_batch_size_is_rejected_before_modification(self):
        record = self.pending(timezone.now() - timedelta(seconds=1))
        before = Request.objects.values().get(pk=record.pk)
        for size in (0, -1, 1001):
            with self.subTest(size=size), self.assertRaises(CommandError):
                call_command("expire_approvals", batch_size=size, stdout=StringIO())
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_command_database_error_is_sanitized(self):
        with patch("gateway.management.commands.expire_approvals.expire_due_approvals", side_effect=DatabaseError("PRIVATE_CONNECTION")):
            with self.assertRaises(CommandError) as caught:
                call_command("expire_approvals", stdout=StringIO())
        self.assertNotIn("PRIVATE_CONNECTION", str(caught.exception))
        self.assertIn("DB 오류", str(caught.exception))
