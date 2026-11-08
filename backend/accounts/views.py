"""세션 인증 API. FE는 같은 출처의 /api/ 경로 또는 개발 프록시로 호출한다."""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import exceptions, serializers
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from .roles import get_service_role
from .validators import username_validator


def error_body(code, message):
    return {"error": {"code": code, "message": message}}


@never_cache
def csrf_failure(request, reason=""):
    # 내부 검증 사유와 토큰 값은 응답에 포함하지 않는다.
    return JsonResponse(
        error_body("CSRF_FAILED", "보안 확인에 실패했습니다. 다시 시도해 주세요."),
        status=403,
    )


class LoginInputSerializer(serializers.Serializer):
    username = serializers.CharField(
        max_length=8, trim_whitespace=False, validators=[username_validator]
    )
    password = serializers.CharField(
        max_length=30, trim_whitespace=False, write_only=True
    )


def session_response(request, user, role):
    return {
        "user": {
            "id": str(user.pk),
            "username": user.username,
            "full_name": user.full_name,
            "role": role,
        },
        "session_expires_at": request.session.get_expiry_date().isoformat(),
    }


@method_decorator(never_cache, name="dispatch")
@method_decorator(csrf_protect, name="dispatch")
class AuthAPIView(APIView):
    """로그인 전 POST도 CSRF를 검사하고 인증 응답은 캐시하지 않는다."""

    renderer_classes = [JSONRenderer]
    parser_classes = [JSONParser]
    permission_classes = [AllowAny]

    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        errors = (
            (exceptions.ParseError, "INVALID_JSON", "올바른 JSON 형식으로 요청해 주세요."),
            (exceptions.UnsupportedMediaType, "UNSUPPORTED_MEDIA_TYPE", "JSON 형식으로 요청해 주세요."),
            (exceptions.MethodNotAllowed, "METHOD_NOT_ALLOWED", "허용되지 않는 요청 방식입니다."),
            (exceptions.ValidationError, "INVALID_INPUT", "입력 항목과 형식을 확인해 주세요."),
            (exceptions.NotAuthenticated, "AUTH_REQUIRED", "로그인이 필요합니다."),
            (exceptions.PermissionDenied, "FORBIDDEN", "요청을 처리할 권한이 없습니다."),
        )
        for exception_type, code, message in errors:
            if isinstance(exc, exception_type):
                response.data = error_body(code, message)
                break
        return response


class CsrfTokenView(AuthAPIView):
    authentication_classes = []

    def get(self, request):
        return Response({"csrf_token": get_token(request._request)})


@method_decorator(sensitive_post_parameters("password"), name="dispatch")
class LoginView(AuthAPIView):
    # DRF의 세션 인증은 익명 요청의 CSRF를 검사하지 않으므로 기반 뷰에서 별도 검사한다.
    authentication_classes = []

    def post(self, request):
        data = request.data
        if (
            not isinstance(data, dict)
            or set(data) != {"username", "password"}
            or not all(isinstance(value, str) for value in data.values())
        ):
            raise exceptions.ValidationError("로그인 ID와 비밀번호를 입력해 주세요.")
        serializer = LoginInputSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(request=request._request, **serializer.validated_data)
        role = get_service_role(user)
        if role is None:
            # 존재 여부, 비활성 상태, 그룹 설정 여부를 구분하여 노출하지 않는다.
            return Response(
                error_body("INVALID_CREDENTIALS", "로그인 ID 또는 비밀번호를 확인해 주세요."),
                status=401,
            )

        login(request._request, user)
        # 초 단위 상대 만료가 아닌 절대 시각을 저장하여 세션 저장 시에도 연장하지 않는다.
        expires_at = timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE)
        request.session.set_expiry(expires_at)
        data = session_response(request, user, role)
        # 로그인 시 CSRF 값이 교체되므로 FE는 이후 POST에 이 값을 사용한다.
        data["csrf_token"] = get_token(request._request)
        return Response(data)


class CurrentUserView(AuthAPIView):
    def get(self, request):
        if not request.user.is_authenticated:
            return Response(error_body("AUTH_REQUIRED", "로그인이 필요합니다."), status=401)
        role = get_service_role(request.user)
        if role is None:
            return Response(error_body("FORBIDDEN", "서비스 이용 권한이 없습니다."), status=403)
        return Response(session_response(request, request.user, role))


class LogoutView(AuthAPIView):
    # 만료·비활성 계정도 같은 CSRF 검사를 거쳐 현재 브라우저의 세션을 종료할 수 있다.
    authentication_classes = []

    def post(self, request):
        logout(request._request)
        return Response(status=204)
