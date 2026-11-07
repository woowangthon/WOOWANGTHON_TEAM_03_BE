"""초기 서비스 관리자 5명을 대화형으로 등록한다. 비밀번호는 파일에 저장하지 않는다."""

import getpass
import sys
import warnings

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction

from accounts.roles import ADMIN_GROUP_NAME, get_service_role

ADMIN_COUNT = 5


class Command(BaseCommand):
    help = "초기 관리자 5명을 등록합니다. --check는 등록 상태만 확인합니다."
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="계정을 변경하지 않고 등록 상태 확인")

    def handle(self, *args, **options):
        try:
            self._handle_registration(check_only=options["check"])
        except (EOFError, KeyboardInterrupt):
            raise CommandError("작업이 중단되었습니다. --check로 등록 상태를 확인하세요.") from None
        except getpass.GetPassWarning:
            raise CommandError("비밀번호를 숨길 수 없는 환경입니다. 대화형 터미널에서 실행하세요.") from None
        except ValidationError as error:
            raise CommandError("등록하지 않았습니다. " + " / ".join(error.messages)) from None
        except DatabaseError:
            raise CommandError(
                "DB 오류로 등록을 완료하지 못했습니다. --check로 상태를 확인한 후 다시 실행하세요."
            ) from None

    def _handle_registration(self, check_only):
        try:
            group = Group.objects.get(name=ADMIN_GROUP_NAME)
        except Group.DoesNotExist:
            raise CommandError("먼저 python manage.py setup_service_groups를 실행하세요.") from None

        admins = self._get_admins(group)
        if check_only:
            self._show_admins(admins)
            self._validate_complete_admins(admins)
            self.stdout.write(self.style.SUCCESS("관리자 5명 등록 상태: 정상"))
            return
        if admins:
            self._validate_complete_admins(admins)
            self.stdout.write("이미 관리자 5명이 등록되어 있습니다. 변경하지 않았습니다.")
            self._show_admins(admins)
            return
        if not sys.stdin.isatty():
            raise CommandError("대화형 터미널에서 실행하세요. 비밀번호를 명령 인수나 파이프로 전달하지 마세요.")

        self.stdout.write("관리자 5명의 정보를 입력하세요. 모든 입력이 유효할 때 한 번에 저장합니다.")
        entries = self._read_entries()
        try:
            created = self._register_admins(entries, group.pk)
        finally:
            # 등록 후에는 명령 객체에 평문 비밀번호 목록을 보관하지 않는다.
            entries.clear()

        message = "초기 관리자 5명 등록 완료" if created else "이미 관리자 5명이 등록되어 있습니다. 변경하지 않았습니다."
        self.stdout.write(self.style.SUCCESS(message))
        self._show_admins(self._get_admins(group))

    def _read_entries(self):
        user_model = get_user_model()
        entries = []
        usernames = set()
        for index in range(1, ADMIN_COUNT + 1):
            self.stdout.write(f"\n관리자 {index}/{ADMIN_COUNT}")
            username = input("로그인 ID (대문자 영문·숫자 8자리): ")
            full_name = input("이름: ")
            candidate = user_model(username=username, full_name=full_name)
            for field_name in ("username", "full_name"):
                field = user_model._meta.get_field(field_name)
                field.clean(getattr(candidate, field_name), candidate)
            if username in usernames:
                raise CommandError("입력한 관리자 ID가 중복됩니다. 이번 등록 내용은 저장하지 않았습니다.")
            if user_model.objects.filter(username=username).exists():
                raise CommandError("이미 사용 중인 ID입니다. 기존 계정은 변경하지 않으며 등록을 중단합니다.")

            # getpass의 화면 표시 방식으로 대체되는 동작을 금지한다.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("초기 비밀번호 (화면에 표시되지 않음): ")
                confirmation = getpass.getpass("초기 비밀번호 확인: ")
            if password != confirmation:
                raise CommandError("비밀번호 확인이 일치하지 않습니다. 이번 등록 내용은 저장하지 않았습니다.")
            validate_password(password, user=candidate)
            entries.append({"username": username, "full_name": full_name, "password": password})
            usernames.add(username)
        return entries

    def _register_admins(self, entries, group_id):
        user_model = get_user_model()
        # 같은 명령을 동시에 실행해도 관리자 5명을 초과하지 않도록 그룹 행을 잠근다.
        # 다른 코드에서 그룹 소속을 직접 변경하는 작업까지 제한하는 DB 제약은 아니다.
        with transaction.atomic():
            group = Group.objects.select_for_update().get(pk=group_id)
            admins = self._get_admins(group)
            if admins:
                self._validate_complete_admins(admins)
                return False
            if user_model.objects.filter(username__in=[entry["username"] for entry in entries]).exists():
                raise CommandError("입력 중 같은 ID가 등록되었습니다. 이번 등록 내용은 저장하지 않았습니다.")
            for entry in entries:
                user = user_model.objects.create_user(
                    **entry, is_active=True, is_staff=False, is_superuser=False
                )
                user.groups.add(group)
        return True

    def _get_admins(self, group):
        return list(get_user_model().objects.filter(groups=group).order_by("username"))

    def _validate_complete_admins(self, admins):
        if len(admins) != ADMIN_COUNT:
            raise CommandError(
                f"현재 관리자 수는 {len(admins)}명입니다. 5명이 아니므로 자동 추가·수정하지 않습니다."
            )
        for admin in admins:
            if (
                get_service_role(admin) != "admin"
                or admin.is_staff
                or admin.is_superuser
                or not admin.has_usable_password()
            ):
                raise CommandError("관리자 계정의 활성 상태·그룹·운영 권한·비밀번호 설정을 확인하세요. 자동 수정하지 않습니다.")

    def _show_admins(self, admins):
        self.stdout.write(f"등록된 서비스 관리자: {len(admins)}/{ADMIN_COUNT}명")
        for admin in admins:
            role = get_service_role(admin) or "미지정 또는 사용 불가"
            self.stdout.write(
                f"{admin.username} | {admin.full_name} | 역할={role} | "
                f"활성={admin.is_active} | staff={admin.is_staff} | superuser={admin.is_superuser}"
            )
