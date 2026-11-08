"""본인 및 관리자의 일반 사용자 이름 변경 API."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, transaction
from django.utils.crypto import constant_time_compare
from rest_framework import exceptions, serializers
from rest_framework.response import Response

from .permissions import IsServiceAdmin, IsServiceMember
from .roles import get_service_role
from .validators import validate_full_name
from .views import AuthAPIView, error_body


class NameInputSerializer(serializers.Serializer):
    full_name = serializers.CharField(trim_whitespace=False, validators=[validate_full_name])

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError({
                "non_field_errors": ["JSON 객체로 요청해 주세요."]
            })
        if set(data) - set(self.fields):
            raise serializers.ValidationError({
                "non_field_errors": ["이름 외의 항목은 변경할 수 없습니다."]
            })
        if "full_name" in data and not isinstance(data["full_name"], str):
            raise serializers.ValidationError({"full_name": ["문자열로 입력해 주세요."]})
        return super().to_internal_value(data)


def save_name(user, full_name):
    user.full_name = full_name
    try:
        # 이름만 저장하여 비밀번호·권한·로그인 ID 등의 변경을 방지한다.
        user.save(update_fields=["full_name"])
    except DjangoValidationError as exc:
        raise serializers.ValidationError({"full_name": exc.messages}) from None


def profile_response(user, role):
    return Response({"user": {
        "id": str(user.pk),
        "username": user.username,
        "full_name": user.full_name,
        "role": role,
        "is_active": user.is_active,
    }})


class NameAPIView(AuthAPIView):
    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if isinstance(exc, serializers.ValidationError):
            response.data["error"]["fields"] = exc.detail
        return response

    @staticmethod
    def database_error_response():
        return Response(
            error_body("NAME_UPDATE_FAILED", "이름을 변경하지 못했습니다. 잠시 후 다시 시도해 주세요."),
            status=503,
        )


class ChangeOwnNameView(NameAPIView):
    permission_classes = [IsServiceMember]

    def patch(self, request):
        serializer = NameInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_model = get_user_model()
        try:
            with transaction.atomic():
                user = user_model.objects.select_for_update().get(pk=request.user.pk)
                # 인증 이후 비밀번호가 재설정된 오래된 요청은 저장하지 않는다.
                if not constant_time_compare(
                    request.user.get_session_auth_hash(), user.get_session_auth_hash()
                ):
                    raise exceptions.NotAuthenticated()
                role = get_service_role(user)
                if role is None:
                    raise exceptions.PermissionDenied()
                save_name(user, serializer.validated_data["full_name"])
        except user_model.DoesNotExist:
            raise exceptions.NotAuthenticated() from None
        except DatabaseError:
            return self.database_error_response()

        # 비밀번호나 세션을 수정하지 않으므로 현재 로그인과 만료 시각을 유지한다.
        return profile_response(user, role)


class ChangeUserNameView(NameAPIView):
    permission_classes = [IsServiceAdmin]

    def patch(self, request, user_id):
        serializer = NameInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_model = get_user_model()
        try:
            with transaction.atomic():
                user = user_model.objects.select_for_update().get(pk=user_id)
                # 관리자의 수정 대상은 기존 재설정 API와 같은 활성 일반 사용자다.
                if get_service_role(user) != "user" or user.is_staff or user.is_superuser:
                    return self.target_not_found_response()
                save_name(user, serializer.validated_data["full_name"])
        except user_model.DoesNotExist:
            return self.target_not_found_response()
        except DatabaseError:
            return self.database_error_response()
        return profile_response(user, "user")

    @staticmethod
    def target_not_found_response():
        return Response(
            error_body("USER_NOT_FOUND", "이름을 변경할 일반 사용자를 찾을 수 없습니다."),
            status=404,
        )
