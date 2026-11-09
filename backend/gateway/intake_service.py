"""접수·준비 단계 수정. 외부 AI 호출과 검사 시작은 이 서비스에 포함하지 않는다."""

import logging
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import DatabaseError, transaction
from django.utils.crypto import constant_time_compare
from rest_framework import exceptions

from accounts.roles import get_service_role

from .intake_validation import IntakeError
from .models import Document, Request
from .storage import document_storage

logger = logging.getLogger(__name__)


def locked_member(authenticated_user):
    """비활성화·비밀번호 변경과 접수가 경합해도 최신 계정 상태로 판단한다."""
    try:
        user = get_user_model().objects.select_for_update().get(pk=authenticated_user.pk)
    except get_user_model().DoesNotExist:
        raise exceptions.NotAuthenticated() from None
    if not user.is_active or not constant_time_compare(
        user.get_session_auth_hash(), authenticated_user.get_session_auth_hash()
    ):
        raise exceptions.NotAuthenticated()
    if get_service_role(user) is None:
        raise exceptions.PermissionDenied()
    return user


def cleanup_unreferenced_file(storage, key):
    """DB 반영 여부가 불명확하면 원본을 지우지 않고 운영 점검 대상으로 남긴다."""
    if key is None:
        return
    try:
        if not Document.objects.filter(storage_object_key=key).exists():
            storage.delete(key)
    except (DatabaseError, OSError):
        # 원문·파일명·저장 경로·예외 상세를 로그로 출력하지 않는다.
        logger.error("접수 실패 후 비공개 파일 정리 여부를 확인해야 합니다.")


def create_intake(authenticated_user, key, instruction, pdf, fingerprint):
    storage = document_storage()
    saved_key = None
    try:
        with transaction.atomic():
            # 같은 사용자의 접수를 직렬화하여 동일 키의 동시 요청도 한 건만 생성한다.
            user = locked_member(authenticated_user)
            existing = Request.objects.select_related("document").filter(
                user=user, client_request_key=key,
            ).first()
            if existing is not None:
                if not existing.intake_fingerprint or not constant_time_compare(
                    existing.intake_fingerprint, fingerprint
                ):
                    raise IntakeError(
                        "REQUEST_KEY_CONFLICT", "다른 입력에는 새 접수 키를 사용해 주세요.", 409,
                    )
                if existing.hidden_at is not None:
                    raise IntakeError("REQUEST_NOT_AVAILABLE", "목록에서 숨긴 요청입니다.", 409)
                return existing, getattr(existing, "document", None), False

            record = Request.objects.create(
                user=user, client_request_key=key, instruction=instruction,
                intake_fingerprint=fingerprint,
            )
            document = None
            if pdf is not None:
                # 사용자 파일명을 경로로 사용하지 않고 요청별로 별도 원본을 보관한다.
                saved_key = f"requests/{record.pk}/{uuid4().hex}.pdf"
                saved_key = storage.save(saved_key, ContentFile(pdf["content"]))
                document = Document.objects.create(
                    request=record, original_filename=pdf["name"],
                    storage_object_key=saved_key, size_bytes=pdf["size_bytes"],
                    page_count=pdf["page_count"], extraction_status=Document.ExtractionStatus.PENDING,
                )
        return record, document, True
    except (DatabaseError, OSError, ValidationError):
        cleanup_unreferenced_file(storage, saved_key)
        raise IntakeError("INTAKE_SAVE_FAILED", "의뢰를 저장하지 못했습니다. 잠시 후 확인해 주세요.", 503) from None


def update_instruction(authenticated_user, request_id, instruction):
    try:
        with transaction.atomic():
            user = locked_member(authenticated_user)
            try:
                record = Request.objects.select_for_update().get(
                    pk=request_id, user=user, hidden_at__isnull=True,
                )
            except Request.DoesNotExist:
                raise IntakeError("REQUEST_NOT_FOUND", "의뢰를 찾을 수 없습니다.", 404) from None
            if record.state != Request.State.PREPARING:
                raise IntakeError("REQUEST_NOT_EDITABLE", "처리 시작 후에는 새 의뢰로 제출해 주세요.", 409)
            document = getattr(record, "document", None)
            if document is not None and document.extraction_status == Document.ExtractionStatus.FAILED:
                raise IntakeError("REQUEST_NOT_EDITABLE", "실패한 파일은 새 의뢰로 제출해 주세요.", 409)
            if document is not None and document.extraction_status == Document.ExtractionStatus.READY:
                if document.extracted_text is None:
                    raise IntakeError("DOCUMENT_NOT_READY", "문서 처리 상태를 확인할 수 없습니다.", 409)
                # 최종 입력 조합 규칙은 AI 연동 시 같은 함수로 통일해야 한다.
                if len(instruction + "\n\n" + document.extracted_text) > settings.INPUT_MAX_CHARS:
                    raise IntakeError("INPUT_TOO_LONG", "의뢰문과 PDF 본문은 합계 60,000문자 이내여야 합니다.")
            if record.instruction != instruction:
                record.instruction = instruction
                record.save(update_fields=["instruction", "updated_at"])
        return record, document
    except (DatabaseError, ValidationError):
        raise IntakeError("REQUEST_UPDATE_FAILED", "의뢰문을 저장하지 못했습니다.", 503) from None
