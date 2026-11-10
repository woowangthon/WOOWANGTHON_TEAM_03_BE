"""승인 대기 중인 본인 의뢰의 저장된 검사 원문·최종 검출 구간을 조회한다."""

from django.db import transaction
from django.utils import timezone

from .intake_validation import IntakeError
from .models import DetectionSpan, Request, SensitiveTerm


def check_preview_available(record):
    if (
        record.state != Request.State.AWAITING_APPROVAL
        or record.inspection_status != Request.InspectionStatus.COMPLETE
        or record.processing_route != Request.ProcessingRoute.EXTERNAL
        or record.approval_status != Request.ApprovalStatus.PENDING
        or record.decided_at is not None
        or record.finished_at is not None
        or record.external_send_started_at is not None
        or record.delivery_status not in (None, Request.DeliveryStatus.NOT_SENT)
    ):
        raise IntakeError("APPROVAL_NOT_AVAILABLE", "현재 사전 확인이 가능한 의뢰가 아닙니다.", 409)
    if record.approval_expires_at is None or not record.input_text.strip():
        raise IntakeError("PREVIEW_NOT_AVAILABLE", "확인 정보를 불러오지 못했습니다.", 503)
    if record.approval_expires_at <= timezone.now():
        # 조회로 기한을 생성·연장하거나 만료 상태를 갱신하지 않는다.
        raise IntakeError("APPROVAL_EXPIRED", "외부 전송 승인 시간이 만료되었습니다.", 409)


def get_preview_spans(record):
    # 복원 토큰·민감정보 사전 ID·다른 의뢰의 값은 사용자 응답에 포함하지 않는다.
    rows = DetectionSpan.objects.filter(request=record).order_by("start_offset", "end_offset", "pk").values(
        "id", "input_source", "start_offset", "end_offset", "sensitive_term__label",
    )
    spans = []
    previous_end = 0
    for row in rows:
        if (
            row["start_offset"] < previous_end
            or row["end_offset"] <= row["start_offset"]
            or row["input_source"] not in DetectionSpan.InputSource.values
            or row["sensitive_term__label"] not in SensitiveTerm.Label.values
        ):
            raise IntakeError("PREVIEW_NOT_AVAILABLE", "확인 정보를 불러오지 못했습니다.", 503)
        # 저장 좌표는 input_text 기준이며 끝 위치는 미포함이다.
        # 문자 단위는 AI 저장자와 합의한 전역 규칙을 따른다. 여기서 변환·재탐지하지 않는다.
        spans.append({
            "id": str(row["id"]), "input_source": row["input_source"],
            "start_offset": row["start_offset"], "end_offset": row["end_offset"],
            "label": row["sensitive_term__label"],
        })
        previous_end = row["end_offset"]
    return spans


@transaction.atomic
def get_approval_preview(user, request_id):
    try:
        # 후속 승인·거절 처리도 같은 부모 행을 잠가 조회 중 상태 전이를 직렬화한다.
        # AI 저장자는 결과 저장을 완료한 뒤 승인 대기로 전환하고 해당 결과를 고정해야 한다.
        record = Request.objects.select_for_update().get(
            pk=request_id, user=user, hidden_at__isnull=True,
        )
    except Request.DoesNotExist:
        raise IntakeError("REQUEST_NOT_FOUND", "의뢰를 찾을 수 없습니다.", 404) from None
    check_preview_available(record)
    spans = get_preview_spans(record)
    # 조회 중 기한이 지난 경우에도 원문을 반환하지 않는다.
    check_preview_available(record)
    return {
        "request_id": str(record.pk), "state": record.state,
        "inspection_status": record.inspection_status, "processing_route": record.processing_route,
        # PDF와 의뢰문을 재결합하지 않고 실제 검사에 사용한 문자열을 그대로 제공한다.
        "input_text": record.input_text,
        "detection_count": len(spans), "detection_spans": spans,
        "approval": {"status": record.approval_status, "expires_at": record.approval_expires_at.isoformat()},
    }
