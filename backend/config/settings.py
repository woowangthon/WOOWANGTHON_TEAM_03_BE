"""로컬 개발 설정. 외부 공개 전 배포 설정을 별도로 확인한다."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "accounts.apps.AccountsConfig",
    "gateway.apps.GatewayConfig",
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["POSTGRES_DB"],
        "USER": os.environ["POSTGRES_USER"],
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "HOST": os.environ.get("POSTGRES_HOST", "db"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "OPTIONS": {"connect_timeout": 5},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "accounts.validators.ServicePasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "accounts.permissions.IsServiceMember",
    ],
}

LANGUAGE_CODE = "ja"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# 원본은 공개 MEDIA_URL로 제공하지 않는다. Docker에서는 별도 영속 볼륨을 사용한다.
PRIVATE_DOCUMENT_ROOT = Path(os.environ.get("PRIVATE_DOCUMENT_ROOT", BASE_DIR / "private_documents"))
PDF_MAX_BYTES = 10_000_000
PDF_MAX_PAGES = 20
INSTRUCTION_MAX_CHARS = 4_000
INPUT_MAX_CHARS = 60_000
FILE_UPLOAD_MAX_MEMORY_SIZE = 256 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024
FILE_UPLOAD_HANDLERS = [
    "gateway.uploads.LimitedPDFUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]

# 모델 서비스(AI 저장소 FastAPI) 연결. 값은 .env 로 준다. 비어 있으면 처리 시작 뒤 검사가 MODEL_UNAVAILABLE 로 실패한다.
MODEL_SERVICE_URL = os.environ.get("MODEL_SERVICE_URL", "")
MODEL_API_KEY = os.environ.get("MODEL_API_KEY", "")
# AI 처리 실행 방식: thread(요청 처리 프로세스의 배경 스레드, 단일 프로세스 전제) 또는 command(manage.py process_requests --loop 가 처리)
GATEWAY_WORKER = os.environ.get("GATEWAY_WORKER", "thread")
GATEWAY_LOCAL_MAX_TOKENS = 1024   # 사내 답변 생성 상한. 넘어서 잘리면 실패로 남긴다.
GATEWAY_STUCK_MINUTES = 15        # 검사 중·처리 중에 이보다 오래 머물면 process_requests 가 실패 처리


# 현재 로컬 환경은 HTTP를 사용한다. HTTPS 배포 시 DJANGO_COOKIE_SECURE=1을 설정한다.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_SAVE_EVERY_REQUEST = False
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = os.environ.get("DJANGO_COOKIE_SECURE", "0") == "1"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_FAILURE_VIEW = "accounts.views.csrf_failure"
