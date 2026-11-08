"""가상 계정으로 이름 변경 권한·세션 유지·과거 요청 관계를 검사한다."""

import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from gateway.models import Request

from .roles import ADMIN_GROUP_NAME, USER_GROUP_NAME


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class NameAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "SecurePass1!"
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin = cls.make_account("ADM58001", cls.admin_group)
        cls.member = cls.make_account("USR58001", cls.user_group)
        cls.other = cls.make_account("USR58002", cls.user_group)

    @classmethod
    def make_account(cls, username, group=None, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, full_name="변경 전 가상 이름", password=cls.password, **kwargs,
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.own_url = reverse("accounts:name-change")
        self.admin_url = self.target_url(self.member)
        self.me_url = reverse("accounts:me")
        self.payload = {"full_name": "변경 후 가상 이름"}

    def target_url(self, user):
        return reverse("accounts_admin:user-name-change", kwargs={"user_id": user.pk})

    def csrf_token(self, client):
        response = client.get(reverse("accounts:csrf"))
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def login_as(self, user, client=None):
        client = client or self.client
        response = client.post(
            reverse("accounts:login"),
            data=json.dumps({"username": user.username, "password": self.password}),
            content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf_token(client),
        )
        self.assertEqual(response.status_code, 200)

    def patch_json(self, url, payload=None, client=None):
        client = client or self.client
        return client.patch(
            url, data=json.dumps(self.payload if payload is None else payload),
            content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf_token(client),
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertNotIn(self.password, response.content.decode())

    def assert_only_name_changed(self, user, before):
        after = get_user_model().objects.values().get(pk=user.pk)
        self.assertEqual(after.pop("full_name"), self.payload["full_name"])
        before.pop("full_name")
        self.assertEqual(after, before)

    def test_member_changes_own_name_without_changing_other_fields_or_sessions(self):
        self.login_as(self.member)
        other_browser = Client(enforce_csrf_checks=True)
        self.login_as(self.member, client=other_browser)
        before = get_user_model().objects.values().get(pk=self.member.pk)
        key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        expiry = Session.objects.get(pk=key).expire_date
        response = self.patch_json(self.own_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user": {
            "id": str(self.member.pk), "username": self.member.username,
            "full_name": self.payload["full_name"], "role": "user", "is_active": True,
        }})
        self.assertIn("no-store", response["Cache-Control"])
        self.assert_only_name_changed(self.member, before)
        self.assertEqual(list(self.member.groups.values_list("name", flat=True)), [USER_GROUP_NAME])
        self.assertEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value, key)
        self.assertEqual(Session.objects.get(pk=key).expire_date, expiry)
        for client in (self.client, other_browser):
            me = client.get(self.me_url)
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json()["user"]["full_name"], self.payload["full_name"])
        self.other.refresh_from_db()
        self.assertEqual(self.other.full_name, "변경 전 가상 이름")

    def test_admin_can_change_own_name_without_django_staff_flags(self):
        self.login_as(self.admin)
        response = self.patch_json(self.own_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "admin")
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.full_name, self.payload["full_name"])
        self.assertFalse(self.admin.is_staff)
        self.assertFalse(self.admin.is_superuser)
        self.assertEqual(self.client.get(self.me_url).status_code, 200)

    def test_admin_changes_member_name_and_preserves_both_sessions(self):
        self.login_as(self.admin)
        target_client = Client(enforce_csrf_checks=True)
        self.login_as(self.member, client=target_client)
        before = get_user_model().objects.values().get(pk=self.member.pk)
        sessions = [(client, client.cookies[settings.SESSION_COOKIE_NAME].value)
                    for client in (self.client, target_client)]
        expiries = {key: Session.objects.get(pk=key).expire_date for _, key in sessions}
        response = self.patch_json(self.admin_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user": {
            "id": str(self.member.pk), "username": self.member.username,
            "full_name": self.payload["full_name"], "role": "user", "is_active": True,
        }})
        self.assert_only_name_changed(self.member, before)
        for client, key in sessions:
            self.assertEqual(client.cookies[settings.SESSION_COOKIE_NAME].value, key)
            self.assertEqual(Session.objects.get(pk=key).expire_date, expiries[key])
            self.assertEqual(client.get(self.me_url).status_code, 200)
        self.assertEqual(target_client.get(self.me_url).json()["user"]["full_name"],
                         self.payload["full_name"])

    def test_list_and_existing_request_use_current_name_without_rewriting_history(self):
        history = Request.objects.create(
            user=self.member, client_request_key=uuid4(), instruction="가상 문서를 요약해 주세요.",
        )
        before = Request.objects.values().get(pk=history.pk)
        self.login_as(self.admin)
        self.assertEqual(self.patch_json(self.admin_url).status_code, 200)
        history = Request.objects.select_related("user").get(pk=history.pk)
        self.assertEqual(history.user_id, self.member.pk)
        self.assertEqual(history.user.full_name, self.payload["full_name"])
        self.assertEqual(Request.objects.values().get(pk=history.pk), before)
        response = self.client.get(reverse("accounts_admin:user-create"), {
            "q": self.payload["full_name"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["results"]], [str(self.member.pk)])

    def test_anonymous_cannot_change_names_and_members_cannot_change_others(self):
        for url in (self.own_url, self.admin_url):
            self.assert_error(self.patch_json(url), 403, "AUTH_REQUIRED")
        self.login_as(self.other)
        self.assert_error(self.patch_json(self.admin_url), 403, "FORBIDDEN")
        self.member.refresh_from_db()
        self.assertEqual(self.member.full_name, "변경 전 가상 이름")

    def test_staff_flags_do_not_grant_admin_name_change_permission(self):
        get_user_model().objects.filter(pk=self.other.pk).update(is_staff=True, is_superuser=True)
        self.login_as(self.other)
        self.assert_error(self.patch_json(self.admin_url), 403, "FORBIDDEN")

    def test_admin_cannot_modify_excluded_targets(self):
        self.login_as(self.admin)
        inactive = self.make_account("USR58003", self.user_group, is_active=False)
        unassigned = self.make_account("NOG58001")
        conflicting = self.make_account("MIX58001", self.user_group)
        conflicting.groups.add(self.admin_group)
        staff = self.make_account("STF58001", self.user_group, is_staff=True)
        superuser = self.make_account("SUP58001", self.user_group, is_superuser=True)
        for target in (self.admin, inactive, unassigned, conflicting, staff, superuser):
            with self.subTest(username=target.username):
                self.assert_error(self.patch_json(self.target_url(target)), 404, "USER_NOT_FOUND")
                target.refresh_from_db()
                self.assertEqual(target.full_name, "변경 전 가상 이름")
        url = reverse("accounts_admin:user-name-change", kwargs={"user_id": uuid4()})
        self.assert_error(self.patch_json(url), 404, "USER_NOT_FOUND")

    def test_empty_missing_or_non_string_name_is_rejected(self):
        for actor, url in ((self.member, self.own_url), (self.admin, self.admin_url)):
            self.login_as(actor)
            for payload in ({}, {"full_name": ""}, {"full_name": "  "}, {"full_name": "\n\t"},
                            {"full_name": "　"}, {"full_name": None}, {"full_name": 123},
                            {"full_name": True}, {"full_name": []}, {"full_name": {}}):
                with self.subTest(url=url, payload=payload):
                    response = self.patch_json(url, payload)
                    self.assert_error(response, 400, "INVALID_INPUT")
                    self.assertIn("full_name", response.json()["error"]["fields"])
        self.member.refresh_from_db()
        self.assertEqual(self.member.full_name, "변경 전 가상 이름")

    def test_additional_identity_password_and_permission_fields_are_rejected(self):
        for actor, url in ((self.member, self.own_url), (self.admin, self.admin_url)):
            self.login_as(actor)
            for key, value in (
                ("id", str(self.other.pk)), ("user_id", str(self.other.pk)),
                ("username", "RENAME01"), ("password", "OtherPass9!"), ("role", "admin"),
                ("is_active", False), ("is_staff", True), ("is_superuser", True),
                ("groups", [self.admin_group.pk]), ("user_permissions", []),
            ):
                with self.subTest(url=url, field=key):
                    self.assert_error(self.patch_json(url, {**self.payload, key: value}),
                                      400, "INVALID_INPUT")
        self.member.refresh_from_db()
        self.assertEqual(self.member.username, "USR58001")
        self.assertEqual(self.member.full_name, "변경 전 가상 이름")
        self.assertTrue(self.member.check_password(self.password))
        self.assertTrue(self.member.is_active)
        self.assertFalse(self.member.is_staff)
        self.assertFalse(self.member.is_superuser)
        self.assertEqual(list(self.member.groups.values_list("name", flat=True)), [USER_GROUP_NAME])

    def test_same_name_and_unicode_name_are_accepted_without_trimming(self):
        for actor, url in ((self.member, self.own_url), (self.admin, self.admin_url)):
            self.login_as(actor)
            for name in ("변경 전 가상 이름", "  山田 하나  "):
                response = self.patch_json(url, {"full_name": name})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["user"]["full_name"], name)
                self.member.refresh_from_db()
                self.assertEqual(self.member.full_name, name)

    def test_csrf_and_origin_are_checked_for_patch(self):
        for actor, url in ((self.member, self.own_url), (self.admin, self.admin_url)):
            self.login_as(actor)
            response = self.client.patch(url, data=json.dumps(self.payload), content_type="application/json")
            self.assert_error(response, 403, "CSRF_FAILED")
            response = self.client.patch(
                url, data=json.dumps(self.payload), content_type="application/json",
                HTTP_X_CSRFTOKEN=self.csrf_token(self.client), HTTP_ORIGIN="https://untrusted.example",
            )
            self.assert_error(response, 403, "CSRF_FAILED")
        self.member.refresh_from_db()
        self.assertEqual(self.member.full_name, "변경 전 가상 이름")

    def test_only_patch_json_is_supported(self):
        for actor, url in ((self.member, self.own_url), (self.admin, self.admin_url)):
            self.login_as(actor)
            self.assert_error(self.client.get(url), 405, "METHOD_NOT_ALLOWED")
            response = self.client.post(
                url, data=json.dumps(self.payload), content_type="application/json",
                HTTP_X_CSRFTOKEN=self.csrf_token(self.client),
            )
            self.assert_error(response, 405, "METHOD_NOT_ALLOWED")
            for raw, media, status, code in (
                ("{", "application/json", 400, "INVALID_JSON"),
                ("[]", "application/json", 400, "INVALID_INPUT"),
                ("null", "application/json", 400, "INVALID_INPUT"),
                ("full_name=test", "text/plain", 415, "UNSUPPORTED_MEDIA_TYPE"),
            ):
                response = self.client.patch(
                    url, data=raw, content_type=media, HTTP_X_CSRFTOKEN=self.csrf_token(self.client),
                )
                self.assert_error(response, status, code)

    def test_inactive_actor_and_revoked_or_conflicting_role_are_denied(self):
        self.login_as(self.member)
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.assert_error(self.patch_json(self.own_url), 403, "AUTH_REQUIRED")
        self.login_as(self.admin)
        self.admin.groups.add(self.user_group)
        for url in (self.own_url, self.admin_url):
            self.assert_error(self.patch_json(url), 403, "FORBIDDEN")
        self.admin.groups.clear()
        self.assert_error(self.patch_json(self.admin_url), 403, "FORBIDDEN")

    def test_expired_session_cannot_change_name(self):
        for actor, url in ((self.member, self.own_url), (self.admin, self.admin_url)):
            self.login_as(actor)
            deadline = self.client.session.get_expiry_date()
            with patch("django.utils.timezone.now", return_value=deadline + timedelta(seconds=1)):
                self.assert_error(self.patch_json(url), 403, "AUTH_REQUIRED")

    def test_password_reset_invalidates_old_session_for_name_changes(self):
        target_client = Client(enforce_csrf_checks=True)
        self.login_as(self.member, client=target_client)
        self.login_as(self.admin)
        response = self.client.post(
            reverse("accounts_admin:password-reset", kwargs={"user_id": self.member.pk}),
            data=json.dumps({"new_password": "ChangedPass9!"}), content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token(self.client),
        )
        self.assertEqual(response.status_code, 204)
        self.assert_error(self.patch_json(self.own_url, client=target_client), 403, "AUTH_REQUIRED")
        self.member.refresh_from_db()
        self.assertEqual(self.member.full_name, "변경 전 가상 이름")

    def test_save_failure_rolls_back_and_keeps_session(self):
        user_model = get_user_model()
        original_save = user_model.save

        def fail_after_save(user, *args, **kwargs):
            original_save(user, *args, **kwargs)
            raise DatabaseError("private storage details")

        for actor, url in ((self.member, self.own_url), (self.admin, self.admin_url)):
            self.login_as(actor)
            before = user_model.objects.values().get(pk=self.member.pk)
            with patch.object(user_model, "save", new=fail_after_save):
                response = self.patch_json(url)
            self.assert_error(response, 503, "NAME_UPDATE_FAILED")
            self.assertNotIn("private storage details", response.content.decode())
            self.assertEqual(user_model.objects.values().get(pk=self.member.pk), before)
            self.assertEqual(self.client.get(self.me_url).status_code, 200)
