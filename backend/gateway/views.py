"""PDF·의뢰 API. 본인 상세에서 완성 답변만 제공하고 검사 원문·복원 대응표는 제외한다."""

from django.core.exceptions import RequestDataTooBig, SuspiciousFileOperation
from django.db import DatabaseError
from django.http import FileResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_sameorigin
from rest_framework import exceptions
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from accounts.permissions import IsServiceMember, IsServiceUser
from accounts.roles import get_service_role
from accounts.views import AuthAPIView, error_body

from .intake_service import create_intake, update_instruction
from .intake_validation import IntakeError, instruction_value, single_values, validate_intake
from .models import Document, Request
from .parsers import LimitedJSONParser
from .request_list import get_request_list_response
from .storage import document_storage


def intake_response(record, document):
    return {
        "request_id": str(record.pk),
        "client_request_key": str(record.client_request_key),
        "instruction": record.instruction,
        "state": record.state,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        # 처리 시작 API와 AI 작업자는 아직 연결하지 않았다.
        "processing_available": False,
        "document": None if document is None else {
            "original_filename": document.original_filename,
            "size_bytes": document.size_bytes,
            "page_count": document.page_count,
            "extraction_status": document.extraction_status,
            "file_url": reverse("gateway:request-file", kwargs={"request_id": record.pk}),
        },
    }


def request_detail_response(record, document):
    # AI 저장자는 복원이 끝난 최종 답변을 저장한 뒤에만 completed로 확정해야 한다.
    # 조회 중 상태를 바꾸거나 답변 생성·외부 전송을 다시 실행하지 않는다.
    answer = None
    if record.state == Request.State.COMPLETED:
        if record.answer_text is None or not record.answer_text.strip():
            raise IntakeError(
                "ANSWER_NOT_AVAILABLE", "답변을 불러오지 못했습니다. 잠시 후 다시 확인해 주세요.", 503,
            )
        answer = record.answer_text
    data = intake_response(record, document)
    data["answer_text"] = answer
    data["finished_at"] = record.finished_at.isoformat() if record.finished_at else None
    return data


class IntakeAPIView(AuthAPIView):
    permission_classes = [IsServiceMember]
    parser_classes = [LimitedJSONParser]

    def handle_exception(self, exc):
        if isinstance(exc, IntakeError):
            return Response(error_body(exc.code, exc.message), status=exc.status)
        if isinstance(exc, RequestDataTooBig):
            return Response(error_body("INPUT_TOO_LARGE", "요청 데이터가 너무 큽니다."), status=413)
        if isinstance(exc, DatabaseError):
            return Response(error_body("REQUEST_READ_FAILED", "의뢰 정보를 불러오지 못했습니다."), status=503)
        response = super().handle_exception(exc)
        if isinstance(exc, exceptions.ValidationError):
            response.data["error"]["fields"] = exc.detail
        elif isinstance(exc, exceptions.ParseError) and "multipart/" in self.request.content_type:
            response.data = error_body("INVALID_INPUT", "파일 업로드 형식을 확인해 주세요.")
        elif isinstance(exc, exceptions.UnsupportedMediaType):
            response.data = error_body("UNSUPPORTED_MEDIA_TYPE", "지원하는 요청 형식으로 보내 주세요.")
        return response


class RequestIntakeView(IntakeAPIView):
    """기존 접수 경로에서 GET 본인 이력 조회와 POST 신규 접수를 제공한다."""

    permission_classes = [IsServiceUser]
    parser_classes = [LimitedJSONParser, MultiPartParser]

    def get(self, request):
        return get_request_list_response(request.user, request.query_params)

    def post(self, request):
        data = request.data
        upload_error = getattr(request._request, "intake_upload_error", None)
        if upload_error == "PDF_TOO_LARGE":
            raise IntakeError("PDF_TOO_LARGE", "PDF는 10MB 이내로 첨부해 주세요.", 413)
        if upload_error:
            raise IntakeError("INVALID_INPUT", "PDF는 한 개만 첨부해 주세요.")
        key, instruction, pdf, fingerprint = validate_intake(data)
        record, document, created = create_intake(request.user, key, instruction, pdf, fingerprint)
        return Response(
            {"request": intake_response(record, document), "replayed": not created},
            status=201 if created else 200,
        )


class RequestIntakeDetailView(IntakeAPIView):
    """본인 의뢰·상태·완성 답변 조회와 준비 중 의뢰문 수정을 허용한다."""

    permission_classes = [IsServiceUser]

    def get(self, request, request_id):
        try:
            record = Request.objects.select_related("document").defer(
                "input_text", "document__extracted_text",
            ).get(
                pk=request_id, user=request.user, hidden_at__isnull=True,
            )
        except Request.DoesNotExist:
            raise IntakeError("REQUEST_NOT_FOUND", "의뢰를 찾을 수 없습니다.", 404) from None
        return Response({"request": request_detail_response(record, getattr(record, "document", None))})

    def patch(self, request, request_id):
        values = single_values(request.data, {"instruction"})
        instruction = instruction_value(values.get("instruction"))
        record, document = update_instruction(request.user, request_id, instruction)
        return Response({"request": intake_response(record, document)})


@method_decorator(xframe_options_sameorigin, name="dispatch")
class RequestFileView(IntakeAPIView):
    def perform_content_negotiation(self, request, force=False):
        # PDF를 요청해도 실패 응답은 공통 JSON으로 반환한다.
        return super().perform_content_negotiation(request, force=True)

    def get(self, request, request_id):
        documents = Document.objects.select_related("request")
        if get_service_role(request.user) != "admin":
            documents = documents.filter(request__user=request.user, request__hidden_at__isnull=True)
        try:
            document = documents.get(request_id=request_id)
        except Document.DoesNotExist:
            raise IntakeError("DOCUMENT_NOT_FOUND", "문서를 찾을 수 없습니다.", 404) from None
        if not document.storage_object_key:
            raise IntakeError("DOCUMENT_NOT_AVAILABLE", "원본 파일을 열 수 없습니다.", 409)
        try:
            source = document_storage().open(document.storage_object_key, "rb")
        except (OSError, SuspiciousFileOperation):
            raise IntakeError("DOCUMENT_UNAVAILABLE", "원본 파일을 불러오지 못했습니다.", 503) from None
        response = FileResponse(
            source, content_type="application/pdf", as_attachment=False,
            filename=document.original_filename,
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "sandbox"
        # never_cache는 FileResponse에도 적용된다. 공개 파일 경로나 서명 URL은 노출하지 않는다.
        return response
