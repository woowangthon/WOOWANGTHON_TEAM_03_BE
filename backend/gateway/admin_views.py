"""관리자 전용 이용 이력 API. 이용자용 답변 조회와 분리한다."""

from rest_framework import serializers

from accounts.permissions import IsServiceAdmin
from accounts.views import AuthAPIView

from .admin_request_list import get_admin_request_list_response


class AdminRequestListView(AuthAPIView):
    permission_classes = [IsServiceAdmin]

    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if isinstance(exc, serializers.ValidationError):
            response.data["error"]["fields"] = exc.detail
        return response

    def get(self, request):
        return get_admin_request_list_response(request.query_params)
