"""가상 계정으로 일반 사용자 발급 API의 권한·검증·저장 원자성을 검사한다."""

import json
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .roles import ADMIN_GROUP_NAME, USER_GROUP_NAME


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CreateUserAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.login_password = "SecurePass1!"
        cls.admin = cls.make_account("ADM00001", cls.admin_group)
        cls.member = cls.make_account("MEM00001", cls.user_group)

    @classmethod
    def make_account(cls, username, group=None, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, full_name="발급 검증용 가상 사용자",
            password=cls.login_password, **kwargs,
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.url = reverse("accounts_admin:user-create")
        self.payload = {
            "username": "REG00001",
            "full_name": "새로운 가상 사용자",
            "password": "NewSecure9!",
        }

    def csrf_token(self, client=None):
        response = (client or self.client).get(reverse("accounts:csrf"))
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def login_as(self, user):
        response = self.client.post(
            reverse("accounts:login"),
            data=json.dumps({"username": user.username, "password": self.login_password}),
            content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf_token(),
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def create_account(self, payload=None):
        return self.client.post(
            self.url, data=json.dumps(self.payload if payload is None else payload),
            content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf_token(),
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json()["error"]["code"], code)
        for password in (self.login_password, self.payload["password"]):
            self.assertNotIn(password, response.content.decode())

    def test_admin_creates_regular_user_without_changing_admin_session(self):
        self.login_as(self.admin)
        key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        expiry = Session.objects.get(pk=key).expire_date
        response = self.create_account()
        self.assertEqual(response.status_code, 201)
        created = get_user_model().objects.get(username=self.payload["username"])
        self.assertEqual(response.json(), {"user": {
            "id": str(created.pk), "username": self.payload["username"],
            "full_name": self.payload["full_name"], "role": "user", "is_active": True,
        }})
        self.assertNotEqual(created.password, self.payload["password"])
        self.assertTrue(created.check_password(self.payload["password"]))
        self.assertEqual(list(created.groups.values_list("name", flat=True)), [USER_GROUP_NAME])
        self.assertFalse(created.is_staff)
        self.assertFalse(created.is_superuser)
        self.assertFalse(created.user_permissions.exists())
        self.assertEqual(self.client.session[SESSION_KEY], str(self.admin.pk))
        self.assertEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value, key)
        self.assertEqual(Session.objects.get(pk=key).expire_date, expiry)
        self.assertIn("no-store", response["Cache-Control"])

        other = Client(enforce_csrf_checks=True)
        login_response = other.post(
            reverse("accounts:login"),
            data=json.dumps({"username": created.username, "password": self.payload["password"]}),
            content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf_token(other),
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(login_response.json()["user"]["role"], "user")
        denied = other.post(
            self.url, data=json.dumps({**self.payload, "username": "REG00002"}),
            content_type="application/json", HTTP_X_CSRFTOKEN=login_response.json()["csrf_token"],
        )
        self.assert_error(denied, 403, "FORBIDDEN")

    def test_anonymous_and_regular_user_cannot_issue_accounts(self):
        count = get_user_model().objects.count()
        self.assert_error(self.create_account(), 403, "AUTH_REQUIRED")
        self.login_as(self.member)
        self.assert_error(self.create_account(), 403, "FORBIDDEN")
        self.assertEqual(get_user_model().objects.count(), count)

    def test_staff_and_superuser_flags_do_not_grant_service_admin_access(self):
        get_user_model().objects.filter(pk=self.member.pk).update(is_staff=True, is_superuser=True)
        self.login_as(self.member)
        self.assert_error(self.create_account(), 403, "FORBIDDEN")
        self.assertFalse(get_user_model().objects.filter(username=self.payload["username"]).exists())

    def test_revoked_or_conflicting_admin_group_is_rejected(self):
        self.login_as(self.admin)
        self.admin.groups.add(self.user_group)
        self.assert_error(self.create_account(), 403, "FORBIDDEN")
        self.admin.groups.clear()
        self.assert_error(self.create_account(), 403, "FORBIDDEN")
        self.assertFalse(get_user_model().objects.filter(username=self.payload["username"]).exists())

    def test_inactive_admin_is_rejected_after_login(self):
        self.login_as(self.admin)
        get_user_model().objects.filter(pk=self.admin.pk).update(is_active=False)
        self.assert_error(self.create_account(), 403, "AUTH_REQUIRED")
        self.assertFalse(get_user_model().objects.filter(username=self.payload["username"]).exists())

    def test_expired_admin_session_cannot_issue_accounts(self):
        self.login_as(self.admin)
        deadline = self.client.session.get_expiry_date()
        with patch("django.utils.timezone.now", return_value=deadline + timedelta(seconds=1)):
            self.assert_error(self.create_account(), 403, "AUTH_REQUIRED")
        self.assertFalse(get_user_model().objects.filter(username=self.payload["username"]).exists())

    def test_role_and_internal_fields_cannot_be_injected(self):
        self.login_as(self.admin)
        for key, value in (
            ("role", "admin"), ("groups", [self.admin_group.pk]),
            ("is_staff", True), ("is_superuser", True), ("is_active", False),
            ("user_permissions", []), ("id", str(self.admin.pk)),
            ("email", "fictional@example.invalid"),
        ):
            with self.subTest(field=key):
                self.assert_error(self.create_account({**self.payload, key: value}), 400, "INVALID_INPUT")
        self.assertFalse(get_user_model().objects.filter(username=self.payload["username"]).exists())
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertEqual(list(self.admin.groups.values_list("name", flat=True)), [ADMIN_GROUP_NAME])

    def test_duplicate_submission_does_not_replace_account_or_password(self):
        self.login_as(self.admin)
        self.assertEqual(self.create_account().status_code, 201)
        created = get_user_model().objects.get(username=self.payload["username"])
        old_hash = created.password
        response = self.create_account({
            **self.payload, "full_name": "덮어쓰기 시도", "password": "OtherSecure8!",
        })
        self.assert_error(response, 409, "USERNAME_ALREADY_EXISTS")
        created.refresh_from_db()
        self.assertEqual(created.password, old_hash)
        self.assertEqual(created.full_name, self.payload["full_name"])
        self.assertEqual(get_user_model().objects.filter(username=created.username).count(), 1)

    def test_inactive_id_and_admin_id_cannot_be_reused(self):
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.login_as(self.admin)
        for username in (self.member.username, self.admin.username):
            with self.subTest(username=username):
                self.assert_error(self.create_account({**self.payload, "username": username}),
                                  409, "USERNAME_ALREADY_EXISTS")
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

    def test_invalid_ids_and_names_are_rejected_without_normalization(self):
        self.login_as(self.admin)
        for field, value in (
            ("username", "reg00001"), ("username", "REG0001"),
            ("username", "REG000001"), ("username", "REG_0001"),
            ("username", "ＲＥＧ００００１"), ("username", "REG00001\n"),
            ("full_name", ""), ("full_name", "  "), ("full_name", "\n"),
        ):
            with self.subTest(field=field, value=value):
                response = self.create_account({**self.payload, field: value})
                self.assert_error(response, 400, "INVALID_INPUT")
                self.assertIn(field, response.json()["error"]["fields"])
        self.assertEqual(get_user_model().objects.count(), 2)

    def test_full_name_length_limit(self):
        self.login_as(self.admin)
        name = "가" * 100
        rejected = self.create_account({**self.payload, "full_name": name + "나"})
        self.assert_error(rejected, 400, "INVALID_INPUT")
        self.assertIn("full_name", rejected.json()["error"]["fields"])
        self.assertFalse(get_user_model().objects.filter(username=self.payload["username"]).exists())

        accepted = self.create_account({**self.payload, "full_name": name})
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(get_user_model().objects.get(username=self.payload["username"]).full_name, name)

    def test_password_policy_applies_to_creation(self):
        self.login_as(self.admin)
        for password in (
            "Aa1!short", "Aa1!" + "b" * 27,
            "lowercase1!", "UPPERCASE1!", "MissingNumber!", "MissingSpecial1",
            "SpacePass1! ", "Forbidden1[", "한글Pass123!", "REG00001Aa!",
        ):
            with self.subTest(length=len(password)):
                response = self.create_account({**self.payload, "password": password})
                self.assert_error(response, 400, "INVALID_INPUT")
                self.assertIn("password", response.json()["error"]["fields"])
                self.assertNotIn(password, response.content.decode())
        self.assertFalse(get_user_model().objects.filter(username=self.payload["username"]).exists())

    def test_password_length_boundaries(self):
        self.login_as(self.admin)
        for username, length in (("REG00010", 10), ("REG00030", 30)):
            with self.subTest(length=length):
                response = self.create_account({
                    **self.payload, "username": username, "password": "Aa1!" + "b" * (length - 4),
                })
                self.assertEqual(response.status_code, 201)

    def test_missing_or_non_string_fields_are_rejected(self):
        self.login_as(self.admin)
        for field in self.payload:
            missing = {key: value for key, value in self.payload.items() if key != field}
            self.assert_error(self.create_account(missing), 400, "INVALID_INPUT")
            for value in (None, 12345678, True, [], {}):
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.assert_error(self.create_account({**self.payload, field: value}),
                                      400, "INVALID_INPUT")
        self.assertFalse(get_user_model().objects.filter(username=self.payload["username"]).exists())

    def test_csrf_and_origin_are_checked_before_creation(self):
        self.login_as(self.admin)
        response = self.client.post(self.url, data=json.dumps(self.payload), content_type="application/json")
        self.assert_error(response, 403, "CSRF_FAILED")
        response = self.client.post(
            self.url, data=json.dumps(self.payload), content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token(), HTTP_ORIGIN="https://untrusted.example",
        )
        self.assert_error(response, 403, "CSRF_FAILED")
        self.assertFalse(get_user_model().objects.filter(username=self.payload["username"]).exists())

    def test_creation_requires_json_and_put_is_not_supported(self):
        self.login_as(self.admin)
        response = self.client.put(
            self.url, data=json.dumps(self.payload), content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token(),
        )
        self.assert_error(response, 405, "METHOD_NOT_ALLOWED")
        for raw, media_type, status, code in (
            ("{", "application/json", 400, "INVALID_JSON"),
            ("[]", "application/json", 400, "INVALID_INPUT"),
            ("null", "application/json", 400, "INVALID_INPUT"),
            ("username=REG00001", "text/plain", 415, "UNSUPPORTED_MEDIA_TYPE"),
        ):
            with self.subTest(code=code):
                response = self.client.post(self.url, data=raw, content_type=media_type,
                                            HTTP_X_CSRFTOKEN=self.csrf_token())
                self.assert_error(response, status, code)

    def test_missing_group_returns_setup_error_without_creating_user(self):
        self.login_as(self.admin)
        self.user_group.delete()
        self.assert_error(self.create_account(), 503, "ACCOUNT_SETUP_REQUIRED")
        self.assertFalse(get_user_model().objects.filter(username=self.payload["username"]).exists())
        self.assertFalse(Group.objects.filter(name=USER_GROUP_NAME).exists())

    def test_group_save_failure_rolls_back_user_and_hides_internal_error(self):
        self.login_as(self.admin)
        group_manager = type(self.admin.groups)
        with patch.object(group_manager, "add", side_effect=DatabaseError("internal database detail")):
            response = self.create_account()
        self.assert_error(response, 503, "ACCOUNT_CREATE_FAILED")
        self.assertNotIn("internal database detail", response.content.decode())
        self.assertFalse(get_user_model().objects.filter(username=self.payload["username"]).exists())
