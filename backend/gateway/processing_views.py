"""처리 시작 API. 접수(TK-62)·승인(TK-63) API 와 같은 세션 인증·CSRF·오류 형식을 쓴다."""

from rest_framework import exceptions
from rest_framework.response import Response

from accounts.permissions import IsServiceUser

from .processing import start_processing
from .views import IntakeAPIView, intake_response


class RequestStartView(IntakeAPIView):
    """본인의 준비 중 의뢰를 검사 대기로 바꾼다. 검사는 배경에서 돌고 결과는 상세 조회(GET)로 확인한다."""

    permission_classes = [IsServiceUser]

    def post(self, request, request_id):
        if request.query_params:
            raise exceptions.ValidationError("이 API는 조회 조건을 받지 않습니다.")
        if request.data not in ({}, None) and request.data != []:
            # 의뢰문 수정은 PATCH 상세 API 로 한다. 여기서 입력을 받아 처리 시작과 섞지 않는다.
            raise exceptions.ValidationError("이 API는 입력 항목을 받지 않습니다.")
        record, document = start_processing(request.user, request_id)
        return Response({"request": intake_response(record, document)}, status=202)
