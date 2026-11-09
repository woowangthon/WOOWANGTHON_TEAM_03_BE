"""API 접근을 위한 공통 권한 검사. 문서 소유권과 응답 필드 제한은 API별로 적용한다."""

from rest_framework.permissions import BasePermission

from .roles import get_service_role


class IsServiceMember(BasePermission):
    """활성 상태인 서비스 관리자 또는 일반 이용자만 허용한다."""

    message = "서비스 이용 권한이 없습니다."

    def has_permission(self, request, view):
        return get_service_role(request.user) is not None


class IsServiceUser(BasePermission):
    """채팅용 API는 활성 상태인 일반 이용자만 허용한다."""

    message = "일반 이용자만 사용할 수 있습니다."

    def has_permission(self, request, view):
        return get_service_role(request.user) == "user"


class IsServiceAdmin(BasePermission):
    """Django 운영 권한과 구분하여 서비스 관리자 그룹만 허용한다."""

    message = "관리자 권한이 필요합니다."

    def has_permission(self, request, view):
        return get_service_role(request.user) == "admin"
