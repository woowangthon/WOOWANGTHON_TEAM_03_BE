import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models, router

from .managers import UserManager
from .validators import username_validator, validate_full_name


class User(AbstractUser):
    """서비스 사용자. 권한과 비밀번호 저장 방식은 Django 표준을 사용한다."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(
        "로그인 ID",
        max_length=8,
        unique=True,
        validators=[username_validator],
        help_text="대문자 영문·숫자 8자리. 생성 후 변경할 수 없습니다.",
        error_messages={"unique": "이미 사용 중인 로그인 ID입니다."},
    )
    full_name = models.TextField("이름", validators=[validate_full_name])

    objects = UserManager()
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        db_table = "users"
        verbose_name = "사용자"
        verbose_name_plural = "사용자"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(username__regex=r"^[A-Z0-9]{8}$"),
                name="users_username_format",
            ),
            models.CheckConstraint(
                condition=~models.Q(full_name=""),
                name="users_full_name_not_empty",
            ),
        ]

    def save(self, *args, **kwargs):
        # save()는 기본적으로 필드 검증을 실행하지 않으므로 프로필 규칙을 검사한다.
        update_fields = kwargs.get("update_fields")
        for field_name in ("username", "full_name"):
            # 기존 이름이 100자를 넘어도 로그인·비밀번호 등 다른 필드는 갱신할 수 있다.
            if update_fields is not None and field_name not in update_fields:
                continue
            field = self._meta.get_field(field_name)
            field.clean(getattr(self, field_name), self)
        database = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        previous_username = (
            type(self)._base_manager.using(database)
            .filter(pk=self.pk)
            .values_list("username", flat=True)
            .first()
        )
        if previous_username is not None and previous_username != self.username:
            raise ValidationError({"username": "로그인 ID는 변경할 수 없습니다."})
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("사용자는 삭제하지 않고 is_active=False로 비활성화합니다.")

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.full_name

    def __str__(self):
        return self.username
