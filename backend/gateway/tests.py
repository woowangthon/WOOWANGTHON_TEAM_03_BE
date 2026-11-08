"""가상 데이터로 관계·중복·복원 원문 보존 규칙을 확인한다."""

import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from .models import DetectionSpan, Document, MaskingMapping, Request, SensitiveTerm


class GatewayModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="USER0001", full_name="가상 사용자", password="SecurePass1!"
        )

    def make_request(self, **overrides):
        values = {"user": self.user, "client_request_key": uuid.uuid4()}
        values.update(overrides)
        return Request.objects.create(**values)

    def make_term(self, **overrides):
        values = {"value": "가상회사", "label": SensitiveTerm.Label.COMPANY}
        values.update(overrides)
        return SensitiveTerm.objects.create(**values)

    def make_span(self, request, term, **overrides):
        values = {
            "request": request,
            "sensitive_term": term,
            "input_source": DetectionSpan.InputSource.INSTRUCTION,
            "start_offset": 0,
            "end_offset": 4,
        }
        values.update(overrides)
        return DetectionSpan.objects.create(**values)

    def test_pdf_can_be_saved_before_instruction(self):
        request = self.make_request()
        document = Document.objects.create(request=request, original_filename="sample.pdf")
        self.assertEqual(request.instruction, "")
        self.assertEqual(request.state, Request.State.PREPARING)
        self.assertEqual(document.pk, request.pk)
        self.assertEqual(document.request.user, self.user)
        self.assertIsNone(document.extracted_text)
        request.instruction = "요약해 주세요."
        request.save()
        request.refresh_from_db()
        self.assertEqual(request.instruction, "요약해 주세요.")

    def test_request_can_exist_without_pdf(self):
        request = self.make_request(instruction="질문에 답변해 주세요.")
        self.assertFalse(Document.objects.filter(request=request).exists())

    def test_second_pdf_for_same_request_is_rejected(self):
        request = self.make_request()
        Document.objects.create(request=request, original_filename="first.pdf")
        with self.assertRaises(ValidationError):
            Document.objects.create(request=request, original_filename="second.pdf")

    def test_client_key_is_unique_per_user(self):
        key = uuid.uuid4()
        self.make_request(client_request_key=key)
        with self.assertRaises(ValidationError):
            self.make_request(client_request_key=key)
        other_user = get_user_model().objects.create_user(
            username="USER0002", full_name="다른 가상 사용자", password="SecurePass2!"
        )
        self.make_request(user=other_user, client_request_key=key)

    def test_input_is_frozen_after_processing_starts(self):
        request = self.make_request(instruction="원래 의뢰문", input_text="원래 의뢰문")
        request.state = Request.State.QUEUED
        request.save()
        request.instruction = "변경된 의뢰문"
        with self.assertRaises(ValidationError):
            request.save()
        request.refresh_from_db()
        self.assertEqual(request.instruction, "원래 의뢰문")

    def test_json_fields_require_expected_shapes(self):
        for overrides in (
            {"warning_codes": {}},
            {"warning_codes": [1]},
            {"inspection_versions": []},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                self.make_request(**overrides)

    def test_failed_request_does_not_store_incomplete_answer(self):
        with self.assertRaises(ValidationError):
            self.make_request(state=Request.State.FAILED, answer_text="불완전한 답변")
        request = self.make_request(
            state=Request.State.FAILED, error_stage="answer_generation", error_code="TIMEOUT"
        )
        self.assertIsNone(request.answer_text)

    def test_document_measurements_reject_invalid_values(self):
        request = self.make_request()
        for values in ({"size_bytes": -1}, {"page_count": 0}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                Document.objects.create(request=request, original_filename="sample.pdf", **values)

    def test_term_value_and_label_pair_is_unique(self):
        self.make_term()
        with self.assertRaises(ValidationError):
            self.make_term()
        self.make_term(label=SensitiveTerm.Label.PROJECT)

    def test_unused_term_can_be_corrected(self):
        term = self.make_term()
        term.value = "수정된 가상회사"
        term.save()
        term.refresh_from_db()
        self.assertEqual(term.value, "수정된 가상회사")

    def test_detected_term_cannot_change_historical_value_or_label(self):
        term = self.make_term()
        self.make_span(self.make_request(input_text="가상회사"), term)
        for field, value in (("value", "변경된 회사"), ("label", SensitiveTerm.Label.PERSON)):
            term.refresh_from_db()
            setattr(term, field, value)
            with self.subTest(field=field), self.assertRaises(ValidationError):
                term.save()

    def test_mapping_also_protects_original_term(self):
        term = self.make_term()
        MaskingMapping.objects.create(
            request=self.make_request(), sensitive_term=term, placeholder="TEST_TOKEN_1"
        )
        term.value = "다른 회사"
        with self.assertRaises(ValidationError):
            term.save()
        with self.assertRaises(ProtectedError):
            term.delete()

    def test_mapping_token_is_scoped_to_request(self):
        term = self.make_term()
        request = self.make_request()
        MaskingMapping.objects.create(request=request, sensitive_term=term, placeholder="TEST_TOKEN_1")
        other_term = self.make_term(value="다른 가상회사")
        with self.assertRaises(ValidationError):
            MaskingMapping.objects.create(
                request=request, sensitive_term=other_term, placeholder="TEST_TOKEN_1"
            )
        MaskingMapping.objects.create(
            request=self.make_request(), sensitive_term=term, placeholder="TEST_TOKEN_1"
        )

    def test_same_term_reuses_one_mapping_within_request(self):
        request = self.make_request(input_text="가상회사 가상회사")
        term = self.make_term()
        mapping = MaskingMapping.objects.create(
            request=request, sensitive_term=term, placeholder="TEST_TOKEN_1"
        )
        self.make_span(request, term, mapping=mapping)
        self.make_span(request, term, mapping=mapping, start_offset=5, end_offset=9)
        self.assertEqual(mapping.detection_spans.count(), 2)
        with self.assertRaises(ValidationError):
            MaskingMapping.objects.create(
                request=request, sensitive_term=term, placeholder="TEST_TOKEN_2"
            )

    def test_mapping_cannot_be_rewritten(self):
        mapping = MaskingMapping.objects.create(
            request=self.make_request(), sensitive_term=self.make_term(), placeholder="TEST_TOKEN_1"
        )
        mapping.placeholder = "TEST_TOKEN_2"
        with self.assertRaises(ValidationError):
            mapping.save()

    def test_span_cannot_reference_other_request_mapping(self):
        term = self.make_term()
        mapping = MaskingMapping.objects.create(
            request=self.make_request(), sensitive_term=term, placeholder="TEST_TOKEN_1"
        )
        with self.assertRaises(ValidationError):
            self.make_span(self.make_request(input_text="가상회사"), term, mapping=mapping)

    def test_span_cannot_reference_other_term_mapping(self):
        request = self.make_request(input_text="가상회사")
        term = self.make_term()
        mapping = MaskingMapping.objects.create(
            request=request,
            sensitive_term=self.make_term(value="다른 가상회사"),
            placeholder="TEST_TOKEN_1",
        )
        with self.assertRaises(ValidationError):
            self.make_span(request, term, mapping=mapping)

    def test_ranges_must_be_positive_nonoverlapping_intervals(self):
        request = self.make_request(input_text="가상회사 관련 가상자료")
        term = self.make_term()
        self.make_span(request, term)
        for start, end in ((-1, 2), (4, 4), (5, 4), (0, 4), (3, 6)):
            with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                self.make_span(request, term, start_offset=start, end_offset=end)
        # 끝 위치는 미포함이므로 바로 이어지는 구간은 허용한다.
        self.make_span(request, term, start_offset=4, end_offset=6)

    def test_request_with_document_cannot_be_cascade_deleted(self):
        request = self.make_request()
        Document.objects.create(request=request, original_filename="sample.pdf")
        with self.assertRaises(ProtectedError):
            request.delete()
