"""접수 입력과 PDF 구조를 검증한다. 텍스트 추출·AI 검사는 수행하지 않는다."""

import hashlib
import json
from io import BytesIO

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from rest_framework import serializers


class IntakeError(Exception):
    def __init__(self, code, message, status=400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(code)


def instruction_value(value, allow_empty=False):
    if not isinstance(value, str):
        raise serializers.ValidationError({"instruction": ["문자열로 입력해 주세요."]})
    if not value and allow_empty:
        return value
    if not value.strip():
        raise serializers.ValidationError({"instruction": ["의뢰문을 입력해 주세요."]})
    if len(value) > settings.INSTRUCTION_MAX_CHARS:
        raise serializers.ValidationError({"instruction": ["의뢰문은 4,000문자 이내로 입력해 주세요."]})
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise serializers.ValidationError({"instruction": ["지원하지 않는 문자가 포함되어 있습니다."]})
    # 하이라이트 좌표에 영향을 주므로 공백·개행·Unicode 정규화를 하지 않는다.
    return value


def single_values(data, allowed):
    if not isinstance(data, dict) or set(data) - allowed:
        raise serializers.ValidationError("허용된 입력 항목만 보내 주세요.")
    if hasattr(data, "getlist"):
        if any(len(data.getlist(key)) != 1 for key in data):
            raise serializers.ValidationError("같은 입력 항목은 한 번만 보내 주세요.")
        return {key: data[key] for key in data}
    return data


def validate_pdf(upload):
    if not isinstance(upload, UploadedFile):
        raise serializers.ValidationError({"pdf": ["PDF 파일을 첨부해 주세요."]})
    name = upload.name
    if (
        not name.lower().endswith(".pdf") or len(name) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or "/" in name or "\\" in name
    ):
        raise IntakeError("INVALID_PDF", "올바른 이름의 PDF 파일을 첨부해 주세요.")
    if upload.content_type not in ("application/pdf", "application/octet-stream"):
        raise IntakeError("INVALID_PDF", "PDF 파일만 접수할 수 있습니다.")
    upload.seek(0)
    content = upload.read(settings.PDF_MAX_BYTES + 1)
    if len(content) > settings.PDF_MAX_BYTES:
        raise IntakeError("PDF_TOO_LARGE", "PDF는 10MB 이내로 첨부해 주세요.", 413)
    if not content.startswith(b"%PDF-"):
        raise IntakeError("INVALID_PDF", "PDF 파일을 읽을 수 없습니다.")
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise IntakeError("PDF_ENCRYPTED", "암호화되지 않은 PDF를 첨부해 주세요.")
        # 명시된 페이지 수를 먼저 확인하고 실제 페이지 구조도 검증한다.
        declared_count = int(reader.trailer["/Root"]["/Pages"].get("/Count", 0))
        if declared_count > settings.PDF_MAX_PAGES:
            raise IntakeError("PDF_PAGE_LIMIT", "PDF는 20쪽 이내로 첨부해 주세요.")
        page_count = len(reader.pages)
        if page_count > settings.PDF_MAX_PAGES:
            raise IntakeError("PDF_PAGE_LIMIT", "PDF는 20쪽 이내로 첨부해 주세요.")
        if page_count < 1:
            raise IntakeError("INVALID_PDF", "페이지가 있는 PDF를 첨부해 주세요.")
    except (PyPdfError, ValueError, TypeError, KeyError, IndexError, RecursionError):
        raise IntakeError("INVALID_PDF", "PDF 파일을 읽을 수 없습니다.") from None
    return {"name": name, "content": content, "size_bytes": len(content), "page_count": page_count}


def validate_intake(data):
    values = single_values(data, {"client_request_key", "instruction", "pdf"})
    key = values.get("client_request_key")
    if not isinstance(key, str):
        raise serializers.ValidationError({"client_request_key": ["UUID 문자열이 필요합니다."]})
    key = serializers.UUIDField().run_validation(key)
    pdf = validate_pdf(values["pdf"]) if "pdf" in values else None
    instruction = instruction_value(values.get("instruction", ""), allow_empty=pdf is not None)
    initial = {
        "instruction": instruction,
        "pdf_name": pdf["name"] if pdf else None,
        "pdf_sha256": hashlib.sha256(pdf["content"]).hexdigest() if pdf else None,
    }
    fingerprint = hashlib.sha256(
        json.dumps(initial, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return key, instruction, pdf, fingerprint
