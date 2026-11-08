"""본인 비밀번호 변경과 관리자의 일반 사용자 비밀번호 재설정 API."""

from django.contrib.auth import get_user_model, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, transaction
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from rest_framework import exceptions, serializers
from rest_framework.response import Response

from .permissions import IsServiceAdmin, IsServiceMember
from .roles import get_service_role
from .views import AuthAPIView, error_body


class PasswordInputSerializer(serializers.Serializer):
    new_password = serializers.CharField(
        min_length=10, max_length=30, trim_whitespace=False, write_only=True
    )

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError({
                "non_field_errors": ["JSON 객체로 요청해 주세요."]
            })
        if set(data) - set(self.fields):
            raise serializers.ValidationError({
                "non_field_errors": ["허용되지 않는 입력 항목이 있습니다."]
            })
        invalid_types = {
            key: ["문자열로 입력해 주세요."]
            for key, value in data.items() if not isinstance(value, str)
        }
        if invalid_types:
            raise serializers.ValidationError(invalid_types)
        return super().to_internal_value(data)


class ChangePasswordSerializer(PasswordInputSerializer):
    current_password = serializers.CharField(
        max_length=30, trim_whitespace=False, write_only=True
    )


@sensitive_variables("new_password")
def save_validated_password(user, new_password):
    # set_password() 자체는 비밀번호 정책을 검사하지 않으므로 저장 전에 검증한다.
    try:
        validate_password(new_password, user=user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({"new_password": exc.messages}) from None
    user.set_password(new_password)
    user.save(update_fields=["password"])


@method_decorator(sensitive_post_parameters("current_password", "new_password"), name="dispatch")
class PasswordAPIView(AuthAPIView):
    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if isinstance(exc, serializers.ValidationError):
            response.data["error"]["fields"] = exc.detail
        return response

    @staticmethod
    def database_error_response():
        return Response(
            error_body("PASSWORD_UPDATE_FAILED", "비밀번호를 변경하지 못했습니다. 잠시 후 다시 시도해 주세요."),
            status=503,
        )


class ChangePasswordView(PasswordAPIView):
    permission_classes = [IsServiceMember]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_model = get_user_model()
        try:
            with transaction.atomic():
                # 동시에 변경·재설정되어도 오래된 비밀번호를 기준으로 덮어쓰지 않는다.
                user = user_model.objects.select_for_update().get(pk=request.user.pk)
                if not constant_time_compare(
                    request.user.get_session_auth_hash(), user.get_session_auth_hash()
                ):
                    raise exceptions.NotAuthenticated()
                if get_service_role(user) is None:
                    raise exceptions.PermissionDenied()
                if not user.check_password(serializer.validated_data["current_password"]):
                    return Response(
                        error_body("CURRENT_PASSWORD_INVALID", "현재 비밀번호를 확인해 주세요."),
                        status=400,
                    )
                save_validated_password(user, serializer.validated_data["new_password"])
        except user_model.DoesNotExist:
            raise exceptions.NotAuthenticated() from None
        except DatabaseError:
            return self.database_error_response()

        # 현재 세션도 종료한다. 다른 세션은 Django의 인증 해시 검증에서 다음 요청을 거부한다.
        # update_session_auth_hash()를 호출하면 현재 세션이 유지되므로 사용하지 않는다.
        logout(request._request)
        return Response(status=204)


class ResetUserPasswordView(PasswordAPIView):
    permission_classes = [IsServiceAdmin]

    def post(self, request, user_id):
        serializer = PasswordInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_model = get_user_model()
        try:
            with transaction.atomic():
                user = user_model.objects.select_for_update().get(pk=user_id)
                # 서비스 관리자와 Django 운영 계정은 이 API로 재설정하지 않는다.
                # 비활성·그룹 미지정·중복 소속 계정도 재설정 대상에서 제외한다.
                if get_service_role(user) != "user" or user.is_staff or user.is_superuser:
                    return self.target_not_found_response()
                save_validated_password(user, serializer.validated_data["new_password"])
        except user_model.DoesNotExist:
            return self.target_not_found_response()
        except DatabaseError:
            return self.database_error_response()

        # 대상 계정의 기존 인증 해시는 무효가 되며 요청한 관리자의 세션은 유지한다.
        return Response(status=204)

    @staticmethod
    def target_not_found_response():
        return Response(
            error_body("USER_NOT_FOUND", "재설정할 일반 사용자를 찾을 수 없습니다."),
            status=404,
        )
