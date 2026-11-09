"""JSON 입력도 파일 업로드와 별도로 크기를 제한한다."""

from io import BytesIO

from django.conf import settings
from rest_framework.parsers import JSONParser

from .intake_validation import IntakeError


class LimitedJSONParser(JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        limit = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        content = stream.read(limit + 1)
        if len(content) > limit:
            raise IntakeError("INPUT_TOO_LARGE", "요청 데이터가 너무 큽니다.", 413)
        return super().parse(BytesIO(content), media_type, parser_context)
