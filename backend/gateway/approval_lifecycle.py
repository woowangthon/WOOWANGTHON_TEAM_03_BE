"""검사 결과 저장 후 승인 대기 전환과 기한 만료를 관리한다. AI 실행은 별도 연결한다."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .approval_preview import check_preview_available, get_preview_spans
from .intake_validation import IntakeError
from .models import Document, Request


APPROVAL_WAIT_MINUTES = 30


def check_approval_preparation(record):
    if (
        record.state != Request.State.ANALYZING
        or record.inspection_status != Request.InspectionStatus.COMPLETE
        or record.processing_route != Request.ProcessingRoute.EXTERNAL
        or record.approval_status is not None
        or record.approval_expires_at is not None
        or record.decided_at is not None
        or record.finished_at is not None
        or record.external_send_started_at is not None
        or record.delivery_status not in (None, Request.DeliveryStatus.NOT_SENT)
        or record.answer_text is not None
    ):
        raise IntakeError("APPROVAL_NOT_AVAILABLE", "승인 대기로 전환할 수 없는 의뢰입니다.", 409)
    if (
        not record.instruction.strip()
        or not record.input_text.strip()
        or len(record.instruction) > settings.INSTRUCTION_MAX_CHARS
        or len(record.input_text) > settings.INPUT_MAX_CHARS
    ):
        raise IntakeError("PREVIEW_NOT_AVAILABLE", "승인 확인용 입력을 확인해 주세요.", 409)
    document = Document.objects.filter(request=record).first()
    if document is not None and (
        document.extraction_status != Document.ExtractionStatus.READY
        or document.extracted_text is None
        or not document.extracted_text.strip()
    ):
        raise IntakeError("PREVIEW_NOT_AVAILABLE", "PDF 텍스트 추출이 완료되지 않았습니다.", 409)
    # 입력을 재결합·정규화하지 않는다. 좌표 단위·원문 일치 검증은 AI 저장자와 연결 시 맞춘다.
    get_preview_spans(record)


@transaction.atomic
def prepare_approval(request_id):
    """클라우드 내부 호출용: 저장된 정상 검사 결과를 확인하고 최초 30분 기한을 설정한다.

    AI 저장자는 같은 Request 행을 잠근 트랜잭션에서 원문·최종 구간·검사 결과를
    모두 저장한 뒤 이 함수를 호출해야 한다. 반환값은 (요청, 새로 전환했는지)이다.
    승인 대기 이후 결과를 수정하거나 재검사 결과로 덮어쓰지 않는다.
    """
    try:
        record = Request.objects.select_for_update().get(pk=request_id)
    except Request.DoesNotExist:
        raise IntakeError("REQUEST_NOT_FOUND", "의뢰를 찾을 수 없습니다.", 404) from None
    if record.state == Request.State.AWAITING_APPROVAL:
        # 중복 완료 알림이나 재호출로 기존 기한을 초기화하지 않는다.
        check_preview_available(record)
        return record, False
    check_approval_preparation(record)
    record.state = Request.State.AWAITING_APPROVAL
    record.approval_status = Request.ApprovalStatus.PENDING
    record.approval_expires_at = timezone.now() + timedelta(minutes=APPROVAL_WAIT_MINUTES)
    record.save(update_fields=["state", "approval_status", "approval_expires_at", "updated_at"])
    return record, True


def due_approvals(cutoff):
    # 숨김은 처리 취소가 아니므로 숨긴 의뢰도 만료 대상에 포함한다.
    # 결정·전송·종료 기록이 있거나 다른 상태인 의뢰는 변경하지 않는다.
    return Request.objects.filter(
        state=Request.State.AWAITING_APPROVAL,
        approval_status=Request.ApprovalStatus.PENDING,
        approval_expires_at__lte=cutoff,
        decided_at__isnull=True,
        finished_at__isnull=True,
        external_send_started_at__isnull=True,
    ).filter(Q(delivery_status__isnull=True) | Q(delivery_status=Request.DeliveryStatus.NOT_SENT))


def expire_due_approvals(*, batch_size=500, dry_run=False):
    """실행 시작 시점까지 만료된 의뢰를 작은 트랜잭션으로 처리하고 반영 건수를 반환한다."""
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("일괄 처리 크기는 1~1000 정수여야 합니다.")
    cutoff = timezone.now()
    if dry_run:
        return due_approvals(cutoff).count()
    total = 0
    while True:
        with transaction.atomic():
            ids = list(
                due_approvals(cutoff).select_for_update(skip_locked=True)
                .order_by("approval_expires_at", "pk").values_list("pk", flat=True)[:batch_size]
            )
            if not ids:
                break
            # 모델 save() 대신 잠금 아래 조건부 갱신한다. 결정 시각·원문·검사 결과는 보존한다.
            total += due_approvals(cutoff).filter(pk__in=ids).update(
                state=Request.State.EXPIRED,
                approval_status=Request.ApprovalStatus.EXPIRED,
                finished_at=F("approval_expires_at"),
                updated_at=timezone.now(),
            )
    # 다른 작업이 잠근 행은 이번 실행에서 건너뛰고 다음 정기 실행에서 다시 확인한다.
    return total
