"""가상 계정으로 비밀번호 변경·재설정 및 기존 세션 무효화를 검사한다."""

import json
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .roles import ADMIN_GROUP_NAME, USER_GROUP_NAME


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PasswordAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.old_password = "SecurePass1!"
        cls.new_password = "ChangedPass9!"
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin = cls.make_account("ADM56001", cls.admin_group)
        cls.member = cls.make_account("USR56001", cls.user_group)
        cls.other = cls.make_account("USR56002", cls.user_group)

    @classmethod
    def make_account(cls, username, group=None, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, full_name="비밀번호 검증용 가상 사용자",
            password=cls.old_password, **kwargs,
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.change_url = reverse("accounts:password-change")
        self.me_url = reverse("accounts:me")
        self.reset_url = self.reset_path(self.member)
        self.change_data = {
            "current_password": self.old_password, "new_password": self.new_password,
        }
        self.reset_data = {"new_password": self.new_password}

    def reset_path(self, user):
        return reverse("accounts_admin:password-reset", kwargs={"user_id": user.pk})

    def csrf_token(self, client):
        response = client.get(reverse("accounts:csrf"))
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def login_as(self, user, client=None, password=None):
        client = client or self.client
        return client.post(
            reverse("accounts:login"),
            data=json.dumps({
                "username": user.username,
                "password": self.old_password if password is None else password,
            }),
            content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf_token(client),
        )

    def signed_in_client(self, user):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(self.login_as(user, client=client).status_code, 200)
        return client

    def post_json(self, url, data, client=None):
        client = client or self.client
        return client.post(
            url, data=json.dumps(data), content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token(client),
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertNotIn(self.old_password, response.content.decode())
        self.assertNotIn(self.new_password, response.content.decode())

    def assert_password_unchanged(self, user):
        user.refresh_from_db()
        self.assertTrue(user.check_password(self.old_password))

    def test_member_changes_password_and_all_old_sessions_are_rejected(self):
        self.client = self.signed_in_client(self.member)
        second = self.signed_in_client(self.member)
        other_user = self.signed_in_client(self.other)
        old_keys = [client.cookies[settings.SESSION_COOKIE_NAME].value
                    for client in (self.client, second)]
        old_hash = get_user_model().objects.get(pk=self.member.pk).password
        response = self.post_json(self.change_url, self.change_data)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertIn("no-store", response["Cache-Control"])
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(self.new_password))
        self.assertFalse(self.member.check_password(self.old_password))
        self.assertNotEqual(self.member.password, self.new_password)
        self.assertNotEqual(self.member.password, old_hash)
        self.assertFalse(Session.objects.filter(pk=old_keys[0]).exists())
        for client, old_key in zip((self.client, second), old_keys):
            # 브라우저에 이전 쿠키가 남아 있어도 서버가 접근을 거부해야 한다.
            client.cookies[settings.SESSION_COOKIE_NAME] = old_key
            self.assert_error(client.get(self.me_url), 401, "AUTH_REQUIRED")
        self.assertEqual(other_user.get(self.me_url).status_code, 200)
        self.assert_error(self.login_as(self.member), 401, "INVALID_CREDENTIALS")
        self.assertEqual(self.login_as(self.member, password=self.new_password).status_code, 200)

    def test_service_admin_can_change_own_password(self):
        self.client = self.signed_in_client(self.admin)
        self.assertEqual(self.post_json(self.change_url, self.change_data).status_code, 204)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(self.new_password))
        self.assert_error(self.client.get(self.me_url), 401, "AUTH_REQUIRED")

    def test_current_password_length_limit(self):
        current = "Aa1!" + "b" * 26
        self.member.set_password(current)
        self.member.save(update_fields=["password"])
        self.assertEqual(self.login_as(self.member, password=current).status_code, 200)

        rejected = self.post_json(self.change_url, {**self.change_data, "current_password": current + "b"})
        self.assert_error(rejected, 400, "INVALID_INPUT")
        message = rejected.json()["error"]["fields"]["current_password"][0]
        self.assertIn("30", message)
        self.assertRegex(message, r"[가-힣]")
        accepted = self.post_json(self.change_url, {**self.change_data, "current_password": current})
        self.assertEqual(accepted.status_code, 204)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(self.new_password))

    def test_wrong_current_password_changes_nothing_and_keeps_session(self):
        self.client = self.signed_in_client(self.member)
        response = self.post_json(self.change_url, {
            **self.change_data, "current_password": "IncorrectPass1!",
        })
        self.assert_error(response, 400, "CURRENT_PASSWORD_INVALID")
        self.assert_password_unchanged(self.member)
        self.assertEqual(self.client.get(self.me_url).status_code, 200)

    def test_admin_reset_invalidates_target_sessions_but_preserves_admin(self):
        self.client = self.signed_in_client(self.admin)
        targets = [self.signed_in_client(self.member), self.signed_in_client(self.member)]
        other = self.signed_in_client(self.other)
        admin_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        admin_expiry = Session.objects.get(pk=admin_key).expire_date
        response = self.post_json(self.reset_url, self.reset_data)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        for client in targets:
            self.assert_error(client.get(self.me_url), 401, "AUTH_REQUIRED")
        self.assertEqual(other.get(self.me_url).status_code, 200)
        self.assertEqual(self.client.get(self.me_url).json()["user"]["id"], str(self.admin.pk))
        self.assertEqual(Session.objects.get(pk=admin_key).expire_date, admin_expiry)
        self.assert_password_unchanged(self.admin)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(self.new_password))
        self.assertEqual(list(self.member.groups.values_list("name", flat=True)), [USER_GROUP_NAME])
        self.assertFalse(self.member.is_staff)
        self.assertFalse(self.member.is_superuser)
        self.assert_error(self.login_as(self.member, client=targets[0]), 401, "INVALID_CREDENTIALS")
        self.assertEqual(self.login_as(self.member, client=targets[0], password=self.new_password).status_code, 200)

    def test_reset_to_same_password_still_revokes_previous_sessions(self):
        self.client = self.signed_in_client(self.admin)
        target = self.signed_in_client(self.member)
        old_hash = get_user_model().objects.get(pk=self.member.pk).password
        self.assertEqual(self.post_json(self.reset_url, {"new_password": self.old_password}).status_code, 204)
        self.member.refresh_from_db()
        self.assertNotEqual(self.member.password, old_hash)
        self.assert_error(target.get(self.me_url), 401, "AUTH_REQUIRED")

    def test_old_session_cannot_change_password_after_admin_reset(self):
        target = self.signed_in_client(self.member)
        self.client = self.signed_in_client(self.admin)
        self.assertEqual(self.post_json(self.reset_url, self.reset_data).status_code, 204)
        response = self.post_json(self.change_url, {
            "current_password": self.new_password, "new_password": "AnotherPass8!",
        }, client=target)
        self.assert_error(response, 403, "AUTH_REQUIRED")
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(self.new_password))

    def test_anonymous_requests_and_regular_user_reset_are_denied(self):
        for url, data in ((self.change_url, self.change_data), (self.reset_url, self.reset_data)):
            self.assert_error(self.post_json(url, data), 403, "AUTH_REQUIRED")
        self.client = self.signed_in_client(self.other)
        self.assert_error(self.post_json(self.reset_url, self.reset_data), 403, "FORBIDDEN")
        self.assert_password_unchanged(self.member)

    def test_staff_flags_do_not_grant_reset_permission(self):
        get_user_model().objects.filter(pk=self.other.pk).update(is_staff=True, is_superuser=True)
        self.client = self.signed_in_client(self.other)
        self.assert_error(self.post_json(self.reset_url, self.reset_data), 403, "FORBIDDEN")
        self.assert_password_unchanged(self.member)

    def test_admin_and_inactive_or_unassigned_targets_cannot_be_reset(self):
        self.client = self.signed_in_client(self.admin)
        unknown = self.make_account("NOG56001")
        get_user_model().objects.filter(pk=self.other.pk).update(is_active=False)
        for target in (self.admin, self.other, unknown):
            with self.subTest(target=target.username):
                self.assert_error(self.post_json(self.reset_path(target), self.reset_data),
                                  404, "USER_NOT_FOUND")
                self.assert_password_unchanged(target)
        self.member.groups.add(self.admin_group)
        self.assert_error(self.post_json(self.reset_url, self.reset_data), 404, "USER_NOT_FOUND")
        self.assert_password_unchanged(self.member)

    def test_django_operator_target_cannot_be_reset(self):
        self.client = self.signed_in_client(self.admin)
        for field in ("is_staff", "is_superuser"):
            get_user_model().objects.filter(pk=self.member.pk).update(is_staff=False, is_superuser=False)
            get_user_model().objects.filter(pk=self.member.pk).update(**{field: True})
            self.assert_error(self.post_json(self.reset_url, self.reset_data), 404, "USER_NOT_FOUND")
        self.assert_password_unchanged(self.member)

    def test_nonexistent_target_is_not_created(self):
        self.client = self.signed_in_client(self.admin)
        count = get_user_model().objects.count()
        url = reverse("accounts_admin:password-reset", kwargs={"user_id": uuid4()})
        self.assert_error(self.post_json(url, self.reset_data), 404, "USER_NOT_FOUND")
        self.assertEqual(get_user_model().objects.count(), count)

    def test_body_cannot_select_other_account_or_modify_roles(self):
        self.client = self.signed_in_client(self.member)
        for key, value in (("user_id", str(self.other.pk)), ("username", self.other.username),
                           ("role", "admin"), ("is_superuser", True)):
            self.assert_error(self.post_json(self.change_url, {**self.change_data, key: value}),
                              400, "INVALID_INPUT")
        self.client = self.signed_in_client(self.admin)
        self.assert_error(self.post_json(self.reset_url, {**self.reset_data, "user_id": str(self.admin.pk)}),
                          400, "INVALID_INPUT")
        for user in (self.member, self.other, self.admin):
            self.assert_password_unchanged(user)

    def test_password_policy_failure_keeps_password_and_sessions(self):
        for actor, url, data in ((self.member, self.change_url, self.change_data),
                                 (self.admin, self.reset_url, self.reset_data)):
            self.client = self.signed_in_client(actor)
            for password in ("Aa1!short", "Aa1!" + "b" * 27, "lowercase1!", "UPPERCASE1!",
                             "MissingNumber!", "MissingSpecial1", "InvalidPass1[", "Has Space1!",
                             "USR56001Aa!"):
                with self.subTest(url=url, length=len(password)):
                    response = self.post_json(url, {**data, "new_password": password})
                    self.assert_error(response, 400, "INVALID_INPUT")
                    self.assertIn("new_password", response.json()["error"]["fields"])
                    self.assertNotIn(password, response.content.decode())
            self.assertEqual(self.client.get(self.me_url).status_code, 200)
            self.assert_password_unchanged(self.member)

    def test_reset_accepts_password_length_boundaries(self):
        self.client = self.signed_in_client(self.admin)
        for length in (10, 30):
            password = "Aa1!" + "b" * (length - 4)
            response = self.post_json(self.reset_url, {"new_password": password})
            self.assertEqual(response.status_code, 204)
            self.member.refresh_from_db()
            self.assertTrue(self.member.check_password(password))

    def test_missing_fields_and_non_string_inputs_are_rejected(self):
        for actor, url, data in ((self.member, self.change_url, self.change_data),
                                 (self.admin, self.reset_url, self.reset_data)):
            self.client = self.signed_in_client(actor)
            for field in data:
                missing = {key: value for key, value in data.items() if key != field}
                self.assert_error(self.post_json(url, missing), 400, "INVALID_INPUT")
                for value in (None, 12345678, True, [], {}):
                    self.assert_error(self.post_json(url, {**data, field: value}), 400, "INVALID_INPUT")
        self.assert_password_unchanged(self.member)

    def test_csrf_origin_method_and_json_requirements(self):
        for actor, url, data in ((self.member, self.change_url, self.change_data),
                                 (self.admin, self.reset_url, self.reset_data)):
            self.client = self.signed_in_client(actor)
            self.assert_error(self.client.get(url), 405, "METHOD_NOT_ALLOWED")
            self.assert_error(self.client.post(url, data=json.dumps(data), content_type="application/json"),
                              403, "CSRF_FAILED")
            response = self.client.post(
                url, data=json.dumps(data), content_type="application/json",
                HTTP_X_CSRFTOKEN=self.csrf_token(self.client), HTTP_ORIGIN="https://untrusted.example",
            )
            self.assert_error(response, 403, "CSRF_FAILED")
            for raw, media, status, code in (("{", "application/json", 400, "INVALID_JSON"),
                                             ("[]", "application/json", 400, "INVALID_INPUT"),
                                             ("null", "application/json", 400, "INVALID_INPUT"),
                                             ("new_password=x", "text/plain", 415, "UNSUPPORTED_MEDIA_TYPE")):
                response = self.client.post(url, data=raw, content_type=media,
                                            HTTP_X_CSRFTOKEN=self.csrf_token(self.client))
                self.assert_error(response, status, code)
        self.assert_password_unchanged(self.member)

    def test_inactive_actor_and_revoked_admin_permissions_are_rejected(self):
        self.client = self.signed_in_client(self.member)
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.assert_error(self.post_json(self.change_url, self.change_data), 403, "AUTH_REQUIRED")
        self.client = self.signed_in_client(self.admin)
        self.admin.groups.clear()
        self.assert_error(self.post_json(self.reset_url, self.reset_data), 403, "FORBIDDEN")
        self.assert_password_unchanged(self.member)

    def test_save_failure_rolls_back_and_hides_database_details(self):
        user_model = get_user_model()
        original_save = user_model.save

        def fail_after_save(user, *args, **kwargs):
            original_save(user, *args, **kwargs)
            raise DatabaseError("internal storage details")

        for actor, url, data in ((self.member, self.change_url, self.change_data),
                                 (self.admin, self.reset_url, self.reset_data)):
            self.client = self.signed_in_client(actor)
            with patch.object(user_model, "save", new=fail_after_save):
                response = self.post_json(url, data)
            self.assert_error(response, 503, "PASSWORD_UPDATE_FAILED")
            self.assertNotIn("internal storage details", response.content.decode())
            self.assert_password_unchanged(self.member)
            self.assertEqual(self.client.get(self.me_url).status_code, 200)
