"""가상 계정으로 비활성화 권한·접근 차단·이력 보존을 검사한다."""

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

from gateway.models import Document, Request

from .roles import ADMIN_GROUP_NAME, USER_GROUP_NAME


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DeactivateUserAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "SecurePass1!"
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin = cls.make_account("ADM59001", cls.admin_group)
        cls.member = cls.make_account("USR59001", cls.user_group)
        cls.other = cls.make_account("USR59002", cls.user_group)

    @classmethod
    def make_account(cls, username, group=None, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, full_name="비활성화 검증용 가상 사용자", password=cls.password, **kwargs,
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.url = self.target_url(self.member)
        self.me_url = reverse("accounts:me")
        self.list_url = reverse("accounts_admin:user-create")

    def target_url(self, user):
        return reverse("accounts_admin:user-deactivate", kwargs={"user_id": user.pk})

    def csrf_token(self, client):
        response = client.get(reverse("accounts:csrf"))
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def login_response(self, user, client):
        return client.post(
            reverse("accounts:login"),
            data=json.dumps({"username": user.username, "password": self.password}),
            content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf_token(client),
        )

    def login_as(self, user, client=None):
        self.assertEqual(self.login_response(user, client or self.client).status_code, 200)

    def post_json(self, url=None, payload=None, client=None):
        client = client or self.client
        return client.post(
            url or self.url, data=json.dumps({} if payload is None else payload),
            content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf_token(client),
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertNotIn(self.password, response.content.decode())

    def test_admin_deactivates_only_target_and_changes_only_active_flag(self):
        self.login_as(self.admin)
        user_model = get_user_model()
        before = user_model.objects.values().get(pk=self.member.pk)
        before_other = user_model.objects.values().get(pk=self.other.pk)
        before_admin = user_model.objects.values().get(pk=self.admin.pk)
        count = user_model.objects.count()
        response = self.post_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user": {
            "id": str(self.member.pk), "username": self.member.username,
            "full_name": self.member.full_name, "role": "user", "is_active": False,
        }})
        self.assertIn("no-store", response["Cache-Control"])
        before["is_active"] = False
        self.assertEqual(user_model.objects.values().get(pk=self.member.pk), before)
        self.assertEqual(user_model.objects.values().get(pk=self.other.pk), before_other)
        self.assertEqual(user_model.objects.values().get(pk=self.admin.pk), before_admin)
        self.assertEqual(user_model.objects.count(), count)
        self.assertEqual(list(self.member.groups.values_list("name", flat=True)), [USER_GROUP_NAME])
        self.assertNotIn(self.member.password, response.content.decode())

    def test_existing_sessions_and_new_login_are_rejected(self):
        targets = [Client(enforce_csrf_checks=True), Client(enforce_csrf_checks=True)]
        for client in targets:
            self.login_as(self.member, client=client)
        other_client = Client(enforce_csrf_checks=True)
        self.login_as(self.other, client=other_client)
        self.login_as(self.admin)
        admin_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        expiry = Session.objects.get(pk=admin_key).expire_date
        self.assertEqual(self.post_json().status_code, 200)
        for client in targets:
            self.assert_error(client.get(self.me_url), 401, "AUTH_REQUIRED")
            response = client.patch(
                reverse("accounts:name-change"), data=json.dumps({"full_name": "변경 시도"}),
                content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf_token(client),
            )
            self.assert_error(response, 403, "AUTH_REQUIRED")
            response = self.post_json(
                reverse("accounts:password-change"),
                {"current_password": self.password, "new_password": "ChangedPass9!"}, client=client,
            )
            self.assert_error(response, 403, "AUTH_REQUIRED")
        self.assert_error(self.login_response(self.member, Client(enforce_csrf_checks=True)),
                          401, "INVALID_CREDENTIALS")
        self.assertEqual(other_client.get(self.me_url).status_code, 200)
        self.assertEqual(self.client.get(self.me_url).status_code, 200)
        self.assertEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value, admin_key)
        self.assertEqual(Session.objects.get(pk=admin_key).expire_date, expiry)

    def test_request_and_document_records_are_preserved_without_interrupting_work(self):
        history = Request.objects.create(
            user=self.member, client_request_key=uuid4(), instruction="가상 문서를 요약해 주세요.",
            state=Request.State.ANALYZING,
        )
        document = Document.objects.create(
            request=history, original_filename="fictional.pdf",
            storage_object_key="test-documents/fictional.pdf", extracted_text="가상 원문",
            extraction_status=Document.ExtractionStatus.READY, page_count=1, size_bytes=100,
        )
        request_before = Request.objects.values().get(pk=history.pk)
        document_before = Document.objects.values().get(pk=document.pk)
        self.login_as(self.admin)
        self.assertEqual(self.post_json().status_code, 200)
        self.assertEqual(Request.objects.values().get(pk=history.pk), request_before)
        self.assertEqual(Document.objects.values().get(pk=document.pk), document_before)
        history = Request.objects.select_related("user").get(pk=history.pk)
        self.assertEqual(history.user_id, self.member.pk)
        self.assertFalse(history.user.is_active)

    def test_inactive_user_is_visible_in_inactive_and_all_lists(self):
        self.login_as(self.admin)
        self.assertEqual(self.post_json().status_code, 200)
        for params, expected in (
            ({}, []), ({"status": "active"}, []),
            ({"status": "inactive"}, [str(self.member.pk)]),
            ({"status": "all"}, [str(self.member.pk)]),
        ):
            response = self.client.get(self.list_url, {"q": self.member.username, **params})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([item["id"] for item in response.json()["results"]], expected)
            for item in response.json()["results"]:
                self.assertFalse(item["is_active"])

    def test_repeated_deactivation_succeeds_without_saving_again(self):
        self.login_as(self.admin)
        first = self.post_json()
        self.assertEqual(first.status_code, 200)
        with patch.object(get_user_model(), "save", side_effect=AssertionError("unexpected user save")):
            second = self.post_json()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first.json())

    def test_request_without_body_is_accepted(self):
        self.login_as(self.admin)
        response = self.client.post(
            self.url, data="", content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token(self.client),
        )
        self.assertEqual(response.status_code, 200)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

    def test_anonymous_and_regular_users_cannot_deactivate_accounts(self):
        self.assert_error(self.post_json(), 403, "AUTH_REQUIRED")
        self.login_as(self.member)
        self.assert_error(self.post_json(), 403, "FORBIDDEN")
        self.assert_error(self.post_json(self.target_url(self.other)), 403, "FORBIDDEN")
        self.member.refresh_from_db()
        self.other.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertTrue(self.other.is_active)

    def test_staff_flags_do_not_grant_deactivation_permission(self):
        get_user_model().objects.filter(pk=self.other.pk).update(is_staff=True, is_superuser=True)
        self.login_as(self.other)
        self.assert_error(self.post_json(), 403, "FORBIDDEN")

    def test_admin_and_operator_or_invalid_group_targets_are_excluded(self):
        self.login_as(self.admin)
        other_admin = self.make_account("ADM59002", self.admin_group)
        unassigned = self.make_account("NOG59001")
        conflicting = self.make_account("MIX59001", self.user_group)
        conflicting.groups.add(self.admin_group)
        staff = self.make_account("STF59001", self.user_group, is_staff=True)
        superuser = self.make_account(
            "SUP59001", self.user_group, is_superuser=True,
        )
        for target in (self.admin, other_admin, unassigned, conflicting, staff, superuser):
            with self.subTest(username=target.username):
                self.assert_error(self.post_json(self.target_url(target)), 404, "USER_NOT_FOUND")
                target.refresh_from_db()
                self.assertTrue(target.is_active)

    def test_missing_target_returns_not_found_without_creating_an_account(self):
        self.login_as(self.admin)
        count = get_user_model().objects.count()
        url = reverse("accounts_admin:user-deactivate", kwargs={"user_id": uuid4()})
        self.assert_error(self.post_json(url), 404, "USER_NOT_FOUND")
        self.assertEqual(get_user_model().objects.count(), count)

    def test_revoked_conflicting_inactive_or_expired_admin_cannot_deactivate(self):
        self.login_as(self.admin)
        deadline = self.client.session.get_expiry_date()
        with patch("django.utils.timezone.now", return_value=deadline + timedelta(seconds=1)):
            self.assert_error(self.post_json(), 403, "AUTH_REQUIRED")
        self.login_as(self.admin)
        self.admin.groups.add(self.user_group)
        self.assert_error(self.post_json(), 403, "FORBIDDEN")
        self.admin.groups.remove(self.user_group)
        self.admin.groups.remove(self.admin_group)
        self.assert_error(self.post_json(), 403, "FORBIDDEN")
        self.admin.groups.add(self.admin_group)
        get_user_model().objects.filter(pk=self.admin.pk).update(is_active=False)
        self.assert_error(self.post_json(), 403, "AUTH_REQUIRED")
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)

    def test_extra_fields_cannot_reactivate_or_modify_other_account_data(self):
        self.login_as(self.admin)
        for payload in (
            {"is_active": True}, {"is_active": False}, {"user_id": str(self.other.pk)},
            {"username": "CHANGE01"}, {"full_name": "이름 변경 시도"},
            {"password": "ChangedPass9!"}, {"role": "admin"}, {"is_superuser": True},
        ):
            with self.subTest(payload=payload):
                self.assert_error(self.post_json(payload=payload), 400, "INVALID_INPUT")
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertEqual(self.post_json().status_code, 200)
        self.assert_error(self.post_json(payload={"is_active": True}), 400, "INVALID_INPUT")
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

    def test_csrf_origin_method_and_json_rules(self):
        self.login_as(self.admin)
        self.assert_error(self.client.post(self.url, data="{}", content_type="application/json"),
                          403, "CSRF_FAILED")
        response = self.client.post(
            self.url, data="{}", content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token(self.client), HTTP_ORIGIN="https://untrusted.example",
        )
        self.assert_error(response, 403, "CSRF_FAILED")
        self.assert_error(self.client.get(self.url), 405, "METHOD_NOT_ALLOWED")
        for method in (self.client.patch, self.client.delete):
            response = method(self.url, data="{}", content_type="application/json",
                              HTTP_X_CSRFTOKEN=self.csrf_token(self.client))
            self.assert_error(response, 405, "METHOD_NOT_ALLOWED")
        for raw, media, status, code in (
            ("{", "application/json", 400, "INVALID_JSON"),
            ("[]", "application/json", 400, "INVALID_INPUT"),
            ("null", "application/json", 400, "INVALID_INPUT"),
            ("false", "application/json", 400, "INVALID_INPUT"),
            ("0", "application/json", 400, "INVALID_INPUT"),
            ("is_active=false", "text/plain", 415, "UNSUPPORTED_MEDIA_TYPE"),
        ):
            response = self.client.post(self.url, data=raw, content_type=media,
                                        HTTP_X_CSRFTOKEN=self.csrf_token(self.client))
            self.assert_error(response, status, code)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)

    def test_inactive_id_cannot_be_reissued(self):
        self.login_as(self.admin)
        self.assertEqual(self.post_json().status_code, 200)
        response = self.post_json(self.list_url, {
            "username": self.member.username, "full_name": "다른 가상 사용자", "password": "NewSecure9!",
        })
        self.assert_error(response, 409, "USERNAME_ALREADY_EXISTS")
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertEqual(self.member.full_name, "비활성화 검증용 가상 사용자")
        self.assertTrue(self.member.check_password(self.password))

    def test_inactive_target_cannot_be_renamed_or_have_password_reset(self):
        self.login_as(self.admin)
        self.assertEqual(self.post_json().status_code, 200)
        response = self.post_json(
            reverse("accounts_admin:password-reset", kwargs={"user_id": self.member.pk}),
            {"new_password": "ChangedPass9!"},
        )
        self.assert_error(response, 404, "USER_NOT_FOUND")
        response = self.client.patch(
            reverse("accounts_admin:user-name-change", kwargs={"user_id": self.member.pk}),
            data=json.dumps({"full_name": "변경 시도"}), content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token(self.client),
        )
        self.assert_error(response, 404, "USER_NOT_FOUND")

    def test_unrelated_group_does_not_prevent_regular_user_deactivation(self):
        self.member.groups.add(Group.objects.create(name="unrelated_group"))
        self.login_as(self.admin)
        self.assertEqual(self.post_json().status_code, 200)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertEqual(self.member.groups.count(), 2)

    def test_database_save_failure_rolls_back_without_revoking_target_access(self):
        self.login_as(self.admin)
        target_client = Client(enforce_csrf_checks=True)
        self.login_as(self.member, client=target_client)
        before = get_user_model().objects.values().get(pk=self.member.pk)
        original_save = get_user_model().save

        def fail_after_save(user, *args, **kwargs):
            original_save(user, *args, **kwargs)
            raise DatabaseError("private storage details")

        with patch.object(get_user_model(), "save", new=fail_after_save):
            response = self.post_json()
        self.assert_error(response, 503, "ACCOUNT_DEACTIVATE_FAILED")
        self.assertNotIn("private storage details", response.content.decode())
        self.assertEqual(get_user_model().objects.values().get(pk=self.member.pk), before)
        self.assertEqual(target_client.get(self.me_url).status_code, 200)
        self.assertEqual(self.client.get(self.me_url).status_code, 200)
