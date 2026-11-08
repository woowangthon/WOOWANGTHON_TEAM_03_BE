import re
import string

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator

USERNAME_PATTERN = r"\A[A-Z0-9]{8}\Z"
username_validator = RegexValidator(
    regex=USERNAME_PATTERN,
    message="로그인 ID는 대문자 영문과 숫자로 구성된 8자리여야 합니다.",
    code="invalid_username",
)

# 전각 괄호는 합의한 문자 그대로 유지한다.
ALLOWED_SPECIAL_CHARACTERS = "!$%&'()*+,.-_|/;:<=>?@［］^{}~"
ALLOWED_PASSWORD_CHARACTERS = frozenset(
    string.ascii_letters + string.digits + ALLOWED_SPECIAL_CHARACTERS
)


def validate_full_name(value):
    if not value or not value.strip():
        raise ValidationError("이름을 입력해 주세요.", code="blank_full_name")


class ServicePasswordValidator:
    def validate(self, password, user=None):
        errors = []
        if not 10 <= len(password) <= 30:
            errors.append(
                ValidationError("비밀번호는 10~30자여야 합니다.", code="password_length")
            )
        if any(character not in ALLOWED_PASSWORD_CHARACTERS for character in password):
            errors.append(
                ValidationError(
                    "비밀번호에는 영문, 숫자, 허용된 특수문자만 사용할 수 있습니다.",
                    code="password_invalid_character",
                )
            )
        checks = (
            (r"[A-Z]", "대문자 영문"),
            (r"[a-z]", "소문자 영문"),
            (r"[0-9]", "숫자"),
        )
        for pattern, description in checks:
            if not re.search(pattern, password):
                errors.append(
                    ValidationError(
                        f"비밀번호에 {description}을 포함해 주세요.",
                        code="password_missing_character_type",
                    )
                )
        if not any(character in ALLOWED_SPECIAL_CHARACTERS for character in password):
            errors.append(
                ValidationError(
                    "비밀번호에 허용된 특수문자를 포함해 주세요.",
                    code="password_missing_special",
                )
            )
        if errors:
            raise ValidationError(errors)

    def get_help_text(self):
        return (
            "비밀번호는 10~30자로, 대문자·소문자 영문·숫자·허용 특수문자를 "
            "모두 포함해야 합니다. 허용 특수문자: " + ALLOWED_SPECIAL_CHARACTERS
        )
