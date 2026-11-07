"""가상 입력으로 초기 관리자 등록의 일괄 저장·재실행·오류 동작을 확인한다."""

import getpass
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .roles import ADMIN_GROUP_NAME, USER_GROUP_NAME, get_service_role

COMMAND_MODULE = "accounts.management.commands.setup_initial_admins"


class InitialAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_service_groups", stdout=StringIO())

    def run_registration(self, inputs=None, passwords=None):
        if inputs is None:
            inputs = [value for i in range(1, 6) for value in (f"ADMIN00{i}", f"가상 관리자 {i}")]
        if passwords is None:
            passwords = ["SecurePass1!"] * 10
        output = StringIO()
        with (
            patch(f"{COMMAND_MODULE}.sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=inputs),
            patch(f"{COMMAND_MODULE}.getpass.getpass", side_effect=passwords),
        ):
            call_command("setup_initial_admins", stdout=output)
        return output.getvalue()

    def test_registers_exactly_five_service_admins_with_hashed_passwords(self):
        output = self.run_registration()
        users = get_user_model().objects.all()
        self.assertEqual(users.count(), 5)
        for user in users:
            self.assertEqual(get_service_role(user), "admin")
            self.assertTrue(user.check_password("SecurePass1!"))
            self.assertNotEqual(user.password, "SecurePass1!")
            self.assertFalse(user.is_staff)
            self.assertFalse(user.is_superuser)
            self.assertEqual(list(user.groups.values_list("name", flat=True)), [ADMIN_GROUP_NAME])
        self.assertIn("초기 관리자 5명 등록 완료", output)
        self.assertNotIn("SecurePass1!", output)

    def test_rerun_does_not_prompt_or_change_existing_accounts(self):
        self.run_registration()
        before = list(get_user_model().objects.order_by("username").values())
        with patch("builtins.input") as prompt, patch(f"{COMMAND_MODULE}.getpass.getpass") as secret:
            output = StringIO()
            call_command("setup_initial_admins", stdout=output)
            prompt.assert_not_called()
            secret.assert_not_called()
        self.assertEqual(before, list(get_user_model().objects.order_by("username").values()))
        self.assertIn("변경하지 않았습니다", output.getvalue())

    def test_check_is_read_only_and_does_not_print_password_hashes(self):
        self.run_registration()
        before = list(get_user_model().objects.order_by("username").values())
        output = StringIO()
        with patch("builtins.input") as prompt, patch(f"{COMMAND_MODULE}.getpass.getpass") as secret:
            call_command("setup_initial_admins", check=True, stdout=output)
            prompt.assert_not_called()
            secret.assert_not_called()
        self.assertIn("등록된 서비스 관리자: 5/5명", output.getvalue())
        self.assertIn("관리자 5명 등록 상태: 정상", output.getvalue())
        for user in before:
            self.assertNotIn(user["password"], output.getvalue())
        self.assertEqual(before, list(get_user_model().objects.order_by("username").values()))

    def test_invalid_last_password_does_not_save_first_four_accounts(self):
        with self.assertRaises(CommandError):
            self.run_registration(passwords=["SecurePass1!"] * 8 + ["short", "short"])
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_mismatched_password_confirmation_saves_nothing(self):
        with self.assertRaises(CommandError):
            self.run_registration(passwords=["SecurePass1!", "DifferentPass2!"])
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_duplicate_id_in_input_saves_nothing(self):
        with self.assertRaises(CommandError):
            self.run_registration(inputs=["ADMIN001", "가상 관리자 1", "ADMIN001", "가상 관리자 2"])
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_invalid_id_or_blank_name_saves_nothing(self):
        for inputs in (["admin001", "가상 관리자"], ["ADMIN001", "  "]):
            with self.subTest(inputs=inputs), self.assertRaises(CommandError):
                self.run_registration(inputs=inputs)
            self.assertEqual(get_user_model().objects.count(), 0)

    def test_existing_general_user_is_not_promoted_or_overwritten(self):
        user = get_user_model().objects.create_user(
            username="ADMIN001", full_name="기존 가상 이용자", password="OriginalPass2!"
        )
        user.groups.add(Group.objects.get(name=USER_GROUP_NAME))
        with self.assertRaises(CommandError):
            self.run_registration()
        user.refresh_from_db()
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(get_service_role(user), "user")
        self.assertEqual(user.full_name, "기존 가상 이용자")
        self.assertTrue(user.check_password("OriginalPass2!"))

    def test_partial_admin_population_is_not_automatically_filled(self):
        user = get_user_model().objects.create_user(
            username="ADMIN001", full_name="기존 가상 관리자", password="SecurePass1!"
        )
        user.groups.add(Group.objects.get(name=ADMIN_GROUP_NAME))
        with patch("builtins.input") as prompt, self.assertRaises(CommandError):
            call_command("setup_initial_admins", stdout=StringIO())
        prompt.assert_not_called()
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_registration_failure_rolls_back_users_and_group_memberships(self):
        manager = get_user_model().objects
        original_create = manager.create_user
        calls = 0

        def fail_on_third_user(**values):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise ValidationError("가상 저장 실패")
            return original_create(**values)

        with patch.object(manager, "create_user", side_effect=fail_on_third_user):
            with self.assertRaises(CommandError):
                self.run_registration()
        self.assertEqual(calls, 3)
        self.assertEqual(get_user_model().objects.count(), 0)
        self.assertEqual(Group.objects.get(name=ADMIN_GROUP_NAME).user_set.count(), 0)

    def test_cancelled_input_saves_nothing(self):
        for interruption in (KeyboardInterrupt(), EOFError()):
            with self.subTest(interruption=type(interruption).__name__), self.assertRaises(CommandError):
                self.run_registration(inputs=["ADMIN001", "가상 관리자 1", interruption])
            self.assertEqual(get_user_model().objects.count(), 0)

    def test_password_echo_fallback_is_rejected(self):
        with self.assertRaises(CommandError):
            self.run_registration(passwords=[getpass.GetPassWarning("가상 입력 환경 오류")])
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_noninteractive_registration_is_rejected(self):
        with patch(f"{COMMAND_MODULE}.sys.stdin.isatty", return_value=False):
            with self.assertRaises(CommandError):
                call_command("setup_initial_admins", stdout=StringIO())
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_missing_admin_group_requires_setup_first(self):
        Group.objects.get(name=ADMIN_GROUP_NAME).delete()
        with self.assertRaisesMessage(CommandError, "setup_service_groups"):
            call_command("setup_initial_admins", stdout=StringIO())
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_more_than_five_admins_is_reported_without_modification(self):
        self.run_registration()
        extra = get_user_model().objects.create_user(
            username="ADMIN006", full_name="추가된 가상 관리자", password="SecurePass1!"
        )
        extra.groups.add(Group.objects.get(name=ADMIN_GROUP_NAME))
        with self.assertRaises(CommandError):
            call_command("setup_initial_admins", stdout=StringIO())
        self.assertEqual(get_user_model().objects.count(), 6)

    def test_check_rejects_unregistered_or_invalid_admin_population(self):
        with self.assertRaises(CommandError):
            call_command("setup_initial_admins", check=True, stdout=StringIO())
        self.run_registration()
        user = get_user_model().objects.get(username="ADMIN001")
        user.groups.add(Group.objects.get(name=USER_GROUP_NAME))
        with self.assertRaises(CommandError):
            call_command("setup_initial_admins", check=True, stdout=StringIO())
