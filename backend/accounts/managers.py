from asgiref.sync import sync_to_async
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import models


class UserQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if {"username", "id", "pk"} & kwargs.keys():
            raise ValidationError("사용자 식별자는 변경할 수 없습니다.")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("사용자는 삭제하지 않고 is_active=False로 비활성화합니다.")


class UserManager(DjangoUserManager.from_queryset(UserQuerySet)):
    """Django의 해시·계정 생성 기능을 유지하면서 입력 규칙을 검사한다."""

    def _validate_account_input(self, username, password, extra_fields):
        # Django의 Unicode 정규화 전에 검사하여 전각 ID 등을 자동 변환하지 않는다.
        user = self.model(username=username, **extra_fields)
        for field_name in ("username", "full_name"):
            field = self.model._meta.get_field(field_name)
            field.clean(getattr(user, field_name), user)
        if not isinstance(password, str) or not password:
            raise ValidationError({"password": "비밀번호를 입력해 주세요."})
        validate_password(password, user=user)

    def create_user(self, username, email=None, password=None, **extra_fields):
        self._validate_account_input(username, password, extra_fields)
        return super().create_user(username, email, password, **extra_fields)

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        # Django 운영용 기능이며 서비스 관리자 5명을 등록하는 용도로 사용하지 않는다.
        self._validate_account_input(username, password, extra_fields)
        return super().create_superuser(username, email, password, **extra_fields)

    async def acreate_user(self, username, email=None, password=None, **extra_fields):
        return await sync_to_async(self.create_user)(
            username, email, password, **extra_fields
        )

    async def acreate_superuser(self, username, email=None, password=None, **extra_fields):
        return await sync_to_async(self.create_superuser)(
            username, email, password, **extra_fields
        )
