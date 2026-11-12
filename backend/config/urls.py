from django.db import DatabaseError, connection
from django.http import HttpResponse, JsonResponse
from django.urls import include, path
from django.views.decorators.http import require_safe


@require_safe
def index(request):
    return HttpResponse(
        '<!doctype html><html lang="ko"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Django 실행 상태 확인</title>'
        '<body style="font-family:system-ui;max-width:640px;margin:64px auto;padding:0 24px">'
        '<h1>Django가 실행 중입니다</h1>'
        '<p>로컬 개발 환경이 실행 중입니다.</p>'
        '<p><a href="/health/">PostgreSQL 연결 상태 확인</a></p>'
        '</body></html>'
    )


@require_safe
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            connected = cursor.fetchone() == (1,)
    except DatabaseError:
        connected = False
    return JsonResponse(
        {"django": "ok", "database": "connected" if connected else "unavailable"},
        status=200 if connected else 503,
    )


urlpatterns = [
    path("api/v1/requests/", include("gateway.urls")),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/admin/", include("accounts.admin_urls")),
    path("api/v1/admin/", include("gateway.admin_urls")),
    path("", index),
    path("health/", health),
]
