from uuid import UUID

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from .validators import ALLOWED_SPECIAL_CHARACTERS, ServicePasswordValidator


class PasswordPolicyTests(SimpleTestCase):
    def setUp(self):
        self.validator = ServicePasswordValidator()

    def test_length_boundaries(self):
        for length in (10, 30):
            with self.subTest(length=length):
                self.validator.validate("Aa1!" + "b" * (length - 4))
        for length in (9, 31):
            with self.subTest(length=length):
                with self.assertRaises(ValidationError):
                    self.validator.validate("Aa1!" + "b" * (length - 4))

    def test_each_required_character_type(self):
        for password in (
            "abcdefgh1!",
            "ABCDEFGH1!",
            "Abcdefghi!",
            "Abcdefghi1",
        ):
            with self.subTest(password=password):
                with self.assertRaises(ValidationError):
                    self.validator.validate(password)

    def test_allowed_special_characters(self):
        for character in ALLOWED_SPECIAL_CHARACTERS:
            with self.subTest(character=character):
                self.validator.validate("Abcdefgh1" + character)

    def test_disallowed_characters_are_rejected(self):
        for character in (" ", "\n", "[", "]", "\\", '"', "`", "한", "😀"):
            with self.subTest(character=character):
                with self.assertRaises(ValidationError):
                    self.validator.validate("Abcdefgh1!" + character)

    def test_service_validator_is_configured(self):
        with self.assertRaises(ValidationError):
            validate_password("Abcdefghi1")


class UserModelTests(TestCase):
    def make_user(self, username="USER0001", **overrides):
        values = {"full_name": "가상 사용자", "password": "SecurePass1!"}
        values.update(overrides)
        return get_user_model().objects.create_user(username=username, **values)

    def test_user_uses_uuid_and_django_password_hash(self):
        user = self.make_user()
        self.assertIsInstance(user.pk, UUID)
        self.assertNotEqual(user.password, "SecurePass1!")
        self.assertTrue(user.check_password("SecurePass1!"))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertEqual(user._meta.db_table, "users")

    def test_username_rejects_invalid_input_without_normalization(self):
        for username in ("user0001", "USER001", "USER00001", "USER_001", "ＵＳＥＲ０００１"):
            with self.subTest(username=username):
                with self.assertRaises(ValidationError):
                    self.make_user(username=username)

    def test_name_is_required(self):
        for full_name in ("", "  ", "\n"):
            with self.subTest(full_name=full_name):
                with self.assertRaises(ValidationError):
                    self.make_user(full_name=full_name)

    def test_password_policy_applies_to_account_creation(self):
        with self.assertRaises(ValidationError):
            self.make_user(password="short")

    def test_username_cannot_change_but_full_name_can(self):
        user = self.make_user()
        original_id = user.pk
        user.full_name = "변경된 가상 사용자"
        user.save(update_fields=["full_name"])
        user.refresh_from_db()
        self.assertEqual(user.full_name, "변경된 가상 사용자")
        self.assertEqual(user.pk, original_id)
        user.username = "USER0002"
        with self.assertRaises(ValidationError):
            user.save()

    def test_inactive_username_cannot_be_reused(self):
        user = self.make_user()
        user.is_active = False
        user.save(update_fields=["is_active"])
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_user()

    def test_django_authentication_rejects_invalid_credentials_and_inactive_user(self):
        user = self.make_user()
        self.assertEqual(
            authenticate(username=user.username, password="SecurePass1!"), user
        )
        self.assertIsNone(authenticate(username=user.username, password="wrong"))
        self.assertIsNone(authenticate(username="MISSING1", password="SecurePass1!"))
        user.is_active = False
        user.save(update_fields=["is_active"])
        self.assertIsNone(authenticate(username=user.username, password="SecurePass1!"))

    def test_queryset_cannot_change_identity(self):
        user = self.make_user()
        with self.assertRaises(ValidationError):
            get_user_model().objects.filter(pk=user.pk).update(username="USER0002")

    def test_user_cannot_be_physically_deleted(self):
        user = self.make_user()
        with self.assertRaises(ValidationError):
            user.delete()
        with self.assertRaises(ValidationError):
            get_user_model().objects.filter(pk=user.pk).delete()
        self.assertTrue(get_user_model().objects.filter(pk=user.pk).exists())
