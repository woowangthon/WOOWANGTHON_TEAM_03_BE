"""외부 전송 승인·거절을 원자적으로 기록한다. AI 호출이나 작업 제출은 수행하지 않는다."""

from django.db import transaction
from django.utils import timezone
from rest_framework import exceptions

from .approval_preview import check_preview_available, get_preview_spans
from .intake_validation import IntakeError
from .models import Request


def validate_approval_decision(data):
    if (
        not isinstance(data, dict)
        or set(data) != {"decision"}
        or not isinstance(data["decision"], str)
        or data["decision"] not in (Request.ApprovalStatus.APPROVED, Request.ApprovalStatus.REJECTED)
    ):
        raise exceptions.ValidationError("decision에 approved 또는 rejected를 입력해 주세요.")
    return data["decision"]


def record_pending_decision(record, decision):
    try:
        check_preview_available(record)
        if decision == Request.ApprovalStatus.APPROVED:
            # 확인 정보 자체가 잘못된 상태에서는 외부 전송 승인을 기록하지 않는다.
            get_preview_spans(record)
        now = timezone.now()
        if record.approval_expires_at <= now:
            raise IntakeError("APPROVAL_EXPIRED", "외부 전송 승인 시간이 만료되었습니다.", 409)
    except IntakeError as exc:
        if exc.code != "APPROVAL_EXPIRED":
            raise
        record.state = Request.State.EXPIRED
        record.approval_status = Request.ApprovalStatus.EXPIRED
        # 이용자가 결정한 시각과 구분한다. 기한이 실제 종료 시각이다.
        record.finished_at = record.approval_expires_at
        record.save(update_fields=["state", "approval_status", "finished_at", "updated_at"])
        return True

    record.approval_status = decision
    record.decided_at = now
    if decision == Request.ApprovalStatus.APPROVED:
        # 기존 AI 처리 연결 전이므로 실제 실행 중 또는 전송 완료로 표시하지 않는다.
        record.state = Request.State.QUEUED
    else:
        record.state = Request.State.REJECTED
        record.finished_at = now
    record.save(update_fields=["state", "approval_status", "decided_at", "finished_at", "updated_at"])
    return False


def decide_approval(user, request_id, decision):
    expired = False
    replayed = False
    with transaction.atomic():
        try:
            record = Request.objects.select_for_update().get(
                pk=request_id, user=user, hidden_at__isnull=True,
            )
        except Request.DoesNotExist:
            raise IntakeError("REQUEST_NOT_FOUND", "의뢰를 찾을 수 없습니다.", 404) from None

        if record.approval_status in (Request.ApprovalStatus.APPROVED, Request.ApprovalStatus.REJECTED):
            if record.decided_at is None:
                raise IntakeError("APPROVAL_NOT_AVAILABLE", "승인 기록을 확인할 수 없습니다.", 409)
            if record.approval_status != decision:
                raise IntakeError("APPROVAL_ALREADY_DECIDED", "이미 처리된 선택은 변경할 수 없습니다.", 409)
            # 기한 이후 또는 후속 처리 완료 이후에도 최초 결정만 반환한다. 작업을 재실행하지 않는다.
            replayed = True
        elif record.approval_status == Request.ApprovalStatus.EXPIRED:
            expired = True
        else:
            expired = record_pending_decision(record, decision)

    # 만료 갱신을 커밋한 뒤 오류를 반환해야 변경이 롤백되지 않는다.
    if expired:
        raise IntakeError("APPROVAL_EXPIRED", "외부 전송 승인 시간이 만료되었습니다.", 409)
    return {
        "request_id": str(record.pk), "state": record.state,
        "approval": {
            "status": record.approval_status,
            "decided_at": record.decided_at.isoformat(),
            "expires_at": record.approval_expires_at.isoformat() if record.approval_expires_at else None,
        },
        "replayed": replayed,
    }
