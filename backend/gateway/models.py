"""요청·문서·민감정보의 저장 구조. 처리 흐름과 외부 API 호출은 별도로 구현한다."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class ValidatedModel(models.Model):
    """일반 저장 시 검증한다. 일괄 갱신과 원시 SQL은 이 검증을 거치지 않는다."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Request(ValidatedModel):
    class State(models.TextChoices):
        PREPARING = "preparing", "입력 준비"
        QUEUED = "queued", "처리 대기"
        ANALYZING = "analyzing", "검사 중"
        AWAITING_APPROVAL = "awaiting_approval", "승인 대기"
        PROCESSING = "processing", "답변 처리 중"
        COMPLETED = "completed", "완료"
        BLOCKED = "blocked", "차단"
        REJECTED = "rejected", "외부 전송 거절"
        EXPIRED = "expired", "승인 기한 만료"
        FAILED = "failed", "처리 실패"

    class InspectionStatus(models.TextChoices):
        PENDING = "pending", "검사 결과 대기"
        COMPLETE = "complete", "정상 완료"
        DEGRADED = "degraded", "불완전 검사"
        FAILED = "failed", "검사 실패"

    class ProcessingRoute(models.TextChoices):
        LOCAL = "local", "내부 AI"
        EXTERNAL = "external", "외부 AI"
        BLOCKED = "blocked", "차단"

    class ApprovalStatus(models.TextChoices):
        PENDING = "pending", "승인 대기"
        APPROVED = "approved", "승인"
        REJECTED = "rejected", "거절"
        EXPIRED = "expired", "기한 만료"

    class DeliveryStatus(models.TextChoices):
        NOT_SENT = "not_sent", "미전송"
        UNKNOWN = "unknown", "전송 여부 확인 불가"
        CONFIRMED_SENT = "confirmed_sent", "전송 확인"

    request_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requests"
    )
    client_request_key = models.UUIDField()
    # PDF만 먼저 제출할 수 있다. AI 처리 시작 조건은 API에서 별도로 검사한다.
    instruction = models.TextField(blank=True, default="")
    input_text = models.TextField(blank=True, default="")
    state = models.CharField(max_length=20, choices=State.choices, default=State.PREPARING)
    inspection_status = models.CharField(
        max_length=10, choices=InspectionStatus.choices, default=InspectionStatus.PENDING
    )
    processing_route = models.CharField(
        max_length=10, choices=ProcessingRoute.choices, null=True, blank=True
    )
    warning_codes = models.JSONField(default=list, blank=True)
    inspection_versions = models.JSONField(default=dict, blank=True)
    error_stage = models.CharField(max_length=64, null=True, blank=True)
    error_code = models.CharField(max_length=100, null=True, blank=True)
    approval_status = models.CharField(
        max_length=10, choices=ApprovalStatus.choices, null=True, blank=True
    )
    approval_expires_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    delivery_status = models.CharField(
        max_length=20, choices=DeliveryStatus.choices, null=True, blank=True
    )
    external_send_started_at = models.DateTimeField(null=True, blank=True)
    answer_text = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    hidden_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "requests"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "client_request_key"], name="requests_user_client_key_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["user", "created_at"], name="requests_user_created_idx"),
            models.Index(fields=["state"], name="requests_state_idx"),
            models.Index(
                fields=["approval_status", "approval_expires_at"],
                name="requests_approval_expiry_idx",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if not isinstance(self.warning_codes, list) or any(
            not isinstance(code, str) or not code.strip() for code in self.warning_codes
        ):
            errors["warning_codes"] = "경고 코드는 비어 있지 않은 문자열 목록이어야 합니다."
        if not isinstance(self.inspection_versions, dict):
            errors["inspection_versions"] = "검사 버전은 JSON 객체여야 합니다."
        if self.answer_text is not None and self.state != self.State.COMPLETED:
            errors["answer_text"] = "정상 완료된 최종 답변만 저장할 수 있습니다."
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            for field in ("user_id", "client_request_key"):
                if getattr(previous, field) != getattr(self, field):
                    errors[field.removesuffix("_id")] = "요청의 소유자와 중복 방지 키는 변경할 수 없습니다."
            if previous.state != self.State.PREPARING:
                for field in ("instruction", "input_text"):
                    if getattr(previous, field) != getattr(self, field):
                        errors[field] = "처리 시작 후 입력은 변경할 수 없습니다. 새 요청을 생성하세요."
        if errors:
            raise ValidationError(errors)


class Document(ValidatedModel):
    class ExtractionStatus(models.TextChoices):
        UPLOADING = "uploading", "업로드 중"
        EXTRACTING = "extracting", "텍스트 추출 중"
        READY = "ready", "추출 완료"
        FAILED = "failed", "업로드 또는 추출 실패"

    request = models.OneToOneField(
        Request, primary_key=True, on_delete=models.PROTECT, related_name="document"
    )
    original_filename = models.TextField()
    # 원본은 비공개 스토리지에 보관한다. 공개 URL이나 임시 서명 URL을 저장하지 않는다.
    storage_object_key = models.TextField(null=True, blank=True)
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    extracted_text = models.TextField(null=True, blank=True)
    extraction_status = models.CharField(
        max_length=10, choices=ExtractionStatus.choices, default=ExtractionStatus.UPLOADING
    )

    class Meta:
        db_table = "documents"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(page_count__isnull=True) | models.Q(page_count__gte=1),
                name="documents_page_count_positive",
            ),
        ]


class SensitiveTerm(ValidatedModel):
    class Label(models.TextChoices):
        COMPANY = "company", "회사명"
        PROJECT = "project", "프로젝트명"
        DOC_NO = "doc_no", "문서 번호"
        PRICE = "price", "금액"
        PERSON = "person", "인명"
        CONTACT = "contact", "연락처"
        BIZ_NO = "biz_no", "사업자 번호"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    value = models.TextField()
    label = models.CharField(max_length=10, choices=Label.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sensitive_terms"
        constraints = [
            models.UniqueConstraint(fields=["value", "label"], name="sensitive_terms_value_label_uniq"),
        ]

    def clean(self):
        super().clean()
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValidationError({"value": "민감정보 원문은 비어 있을 수 없습니다."})
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            changed = previous.value != self.value or previous.label != self.label
            if changed and (self.detection_spans.exists() or self.masking_mappings.exists()):
                # 과거 검출 분류와 답변 복원값을 보존하기 위해 새 항목으로 등록한다.
                raise ValidationError("이미 참조된 민감정보는 변경할 수 없습니다. 새 항목을 등록하세요.")


class MaskingMapping(ValidatedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(Request, on_delete=models.PROTECT, related_name="masking_mappings")
    sensitive_term = models.ForeignKey(
        SensitiveTerm, on_delete=models.PROTECT, related_name="masking_mappings"
    )
    # AI가 사용하는 토큰 형식은 연동 시 확정한다. 복원 원문은 sensitive_term.value이다.
    placeholder = models.TextField()

    class Meta:
        db_table = "masking_mappings"
        constraints = [
            models.UniqueConstraint(
                fields=["request", "placeholder"], name="mappings_request_token_uniq"
            ),
            models.UniqueConstraint(
                fields=["request", "sensitive_term"], name="mappings_request_term_uniq"
            ),
        ]

    def clean(self):
        super().clean()
        if not isinstance(self.placeholder, str) or not self.placeholder.strip():
            raise ValidationError({"placeholder": "복원용 토큰은 비어 있을 수 없습니다."})
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            if any(
                getattr(previous, field) != getattr(self, field)
                for field in ("request_id", "sensitive_term_id", "placeholder")
            ):
                raise ValidationError("저장된 복원 대응표는 변경할 수 없습니다.")


class DetectionSpan(ValidatedModel):
    class InputSource(models.TextChoices):
        INSTRUCTION = "instruction", "의뢰문"
        PDF = "pdf", "PDF 본문"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(Request, on_delete=models.PROTECT, related_name="detection_spans")
    sensitive_term = models.ForeignKey(
        SensitiveTerm, on_delete=models.PROTECT, related_name="detection_spans"
    )
    input_source = models.CharField(max_length=11, choices=InputSource.choices)
    # input_text 기준의 끝 위치 미포함 구간이다. 문자 단위는 AI와 합의한 전역 규칙을 따른다.
    start_offset = models.PositiveIntegerField()
    end_offset = models.PositiveIntegerField()
    mapping = models.ForeignKey(
        MaskingMapping, on_delete=models.PROTECT, related_name="detection_spans", null=True, blank=True
    )

    class Meta:
        db_table = "detection_spans"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_offset__gt=models.F("start_offset")),
                name="spans_end_after_start",
            ),
            models.UniqueConstraint(
                fields=["request", "start_offset", "end_offset"], name="spans_request_range_uniq"
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.mapping_id:
            mapping = MaskingMapping.objects.filter(pk=self.mapping_id).first()
            if mapping is not None and (
                mapping.request_id != self.request_id
                or mapping.sensitive_term_id != self.sensitive_term_id
            ):
                errors["mapping"] = "같은 요청·민감정보의 복원 대응표만 연결할 수 있습니다."
        if self.request_id and self.start_offset is not None and self.end_offset is not None:
            overlaps = type(self).objects.filter(
                request_id=self.request_id,
                start_offset__lt=self.end_offset,
                end_offset__gt=self.start_offset,
            ).exclude(pk=self.pk)
            if overlaps.exists():
                errors["start_offset"] = "같은 요청의 최종 검출 구간은 서로 겹칠 수 없습니다."
        # 문자 단위 확정 전에는 Python len()으로 위치 범위를 임의 해석하지 않는다.
        if errors:
            raise ValidationError(errors)
