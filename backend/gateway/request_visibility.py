"""본인 이력 숨김만 처리한다. 의뢰 취소·파일 삭제·승인 변경은 수행하지 않는다."""

from django.db import transaction
from django.utils import timezone

from .intake_service import locked_request_user
from .intake_validation import IntakeError
from .models import Request


@transaction.atomic
def hide_request(authenticated_user, request_id):
    # 접수·수정과 같은 사용자 → 의뢰 순서로 잠그고 최신 계정·세션 상태를 다시 확인한다.
    user = locked_request_user(authenticated_user)
    try:
        # 반복 숨김은 허용하되 타인의 존재 여부는 노출하지 않는다. 원문·답변은 읽지 않는다.
        record = Request.objects.select_for_update().only("request_id", "hidden_at").get(
            pk=request_id, user=user,
        )
    except Request.DoesNotExist:
        raise IntakeError("REQUEST_NOT_FOUND", "의뢰를 찾을 수 없습니다.", 404) from None
    if record.hidden_at is not None:
        return record.hidden_at
    hidden_at = timezone.now()
    # 상태·승인·전송 결과를 덮어쓰지 않도록 표시 관련 필드만 갱신한다.
    Request.objects.filter(pk=record.pk).update(hidden_at=hidden_at, updated_at=hidden_at)
    return hidden_at
