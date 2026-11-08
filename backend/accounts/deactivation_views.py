"""관리자의 일반 사용자 비활성화 API. 계정과 이력은 삭제하지 않는다."""

from django.contrib.auth import get_user_model
from django.db import DatabaseError, transaction
from rest_framework import serializers
from rest_framework.response import Response

from .permissions import IsServiceAdmin
from .roles import SERVICE_GROUP_NAMES, USER_GROUP_NAME
from .views import AuthAPIView, error_body


class DeactivateUserView(AuthAPIView):
    permission_classes = [IsServiceAdmin]

    def post(self, request, user_id):
        # 대상은 URL로만 지정한다. 활성화나 다른 필드 변경은 허용하지 않는다.
        data = request.data
        if not isinstance(data, dict) or data:
            raise serializers.ValidationError("요청 본문은 생략하거나 빈 JSON 객체로 보내 주세요.")

        user_model = get_user_model()
        try:
            with transaction.atomic():
                user = user_model.objects.select_for_update().get(pk=user_id)
                service_groups = set(
                    user.groups.filter(name__in=SERVICE_GROUP_NAMES).values_list("name", flat=True)
                )
                # 비활성 계정의 반복 요청도 허용하므로 is_active와 그룹 검사를 분리한다.
                if service_groups != {USER_GROUP_NAME} or user.is_staff or user.is_superuser:
                    return self.target_not_found_response()
                if user.is_active:
                    user.is_active = False
                    user.save(update_fields=["is_active"])
        except user_model.DoesNotExist:
            return self.target_not_found_response()
        except DatabaseError:
            return Response(
                error_body("ACCOUNT_DEACTIVATE_FAILED", "계정을 비활성화하지 못했습니다. 잠시 후 다시 시도해 주세요."),
                status=503,
            )

        # Django는 다음 요청에서 비활성 사용자의 기존 세션 인증도 거부한다.
        # 비밀번호·세션 전체·요청 이력·문서에는 변경을 가하지 않는다.
        return Response({"user": {
            "id": str(user.pk),
            "username": user.username,
            "full_name": user.full_name,
            "role": "user",
            "is_active": False,
        }})

    @staticmethod
    def target_not_found_response():
        return Response(
            error_body("USER_NOT_FOUND", "비활성화할 일반 사용자를 찾을 수 없습니다."),
            status=404,
        )
