"""서비스 관리자의 일반 사용자 목록·검색·발급 API."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import serializers
from rest_framework.response import Response

from .permissions import IsServiceAdmin
from .roles import USER_GROUP_NAME
from .user_list import get_user_list_response
from .validators import username_validator, validate_full_name
from .views import AuthAPIView, error_body


class CreateUserSerializer(serializers.Serializer):
    username = serializers.CharField(
        max_length=8, trim_whitespace=False, validators=[username_validator]
    )
    full_name = serializers.CharField(
        trim_whitespace=False, validators=[validate_full_name]
    )
    password = serializers.CharField(
        min_length=10, max_length=30, trim_whitespace=False, write_only=True
    )

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError({
                "non_field_errors": ["JSON 객체로 요청해 주세요."]
            })
        # Serializer가 미정의 필드를 무시하지 않도록 권한 조작 입력도 명시적으로 거부한다.
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

    def validate(self, attrs):
        candidate = get_user_model()(
            username=attrs["username"], full_name=attrs["full_name"]
        )
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": exc.messages}) from None
        return attrs


@method_decorator(sensitive_post_parameters("password"), name="dispatch")
class CreateUserView(AuthAPIView):
    """기존 발급 경로에서 GET 목록 조회와 POST 계정 발급을 제공한다."""

    permission_classes = [IsServiceAdmin]

    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if isinstance(exc, serializers.ValidationError):
            # 입력값을 되돌려 주지 않고 필드별 검증 메시지만 제공한다.
            response.data["error"]["fields"] = exc.detail
        return response

    def get(self, request):
        return get_user_list_response(request.query_params)

    def post(self, request):
        serializer = CreateUserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        user_model = get_user_model()
        try:
            # 계정 저장과 그룹 등록을 하나의 트랜잭션으로 처리한다.
            with transaction.atomic():
                group = Group.objects.get(name=USER_GROUP_NAME)
                user = user_model.objects.create_user(
                    username=values["username"],
                    full_name=values["full_name"],
                    password=values["password"],
                    is_active=True,
                    is_staff=False,
                    is_superuser=False,
                )
                user.groups.add(group)
        except Group.DoesNotExist:
            return Response(
                error_body("ACCOUNT_SETUP_REQUIRED", "계정 발급 준비가 완료되지 않았습니다."),
                status=503,
            )
        except DjangoValidationError as exc:
            # 모델 관리자의 검증도 유지하여 다른 생성 경로와 같은 정책을 적용한다.
            details = getattr(exc, "message_dict", {"non_field_errors": exc.messages})
            raise serializers.ValidationError(details) from None
        except IntegrityError:
            # 중복 검사는 DB의 고유 제약을 기준으로 하며 동시 요청도 처리한다.
            # atomic 블록을 벗어나 롤백한 뒤에 조회해야 한다.
            try:
                duplicate = user_model.objects.filter(username=values["username"]).exists()
            except DatabaseError:
                duplicate = False
            if duplicate:
                return Response(
                    error_body("USERNAME_ALREADY_EXISTS", "이미 사용 중인 로그인 ID입니다."),
                    status=409,
                )
            return self.database_error_response()
        except DatabaseError:
            return self.database_error_response()

        # 새 사용자로 로그인하지 않으며 발급한 관리자의 세션과 만료 시각을 유지한다.
        return Response({
            "user": {
                "id": str(user.pk),
                "username": user.username,
                "full_name": user.full_name,
                "role": "user",
                "is_active": user.is_active,
            }
        }, status=201)

    @staticmethod
    def database_error_response():
        return Response(
            error_body("ACCOUNT_CREATE_FAILED", "계정을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요."),
            status=503,
        )
