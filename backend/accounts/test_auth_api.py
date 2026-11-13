"""가상 계정으로 실제 URL·쿠키·CSRF·DB 세션의 동작을 검사한다."""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .roles import ADMIN_GROUP_NAME, USER_GROUP_NAME


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.password = "SecurePass1!"
        cls.user = cls.make_user("AUTH0001", cls.user_group)
        cls.admin = cls.make_user("AUTH0002", cls.admin_group)
        cls.inactive = cls.make_user("AUTH0003", cls.user_group, is_active=False)
        cls.ungrouped = cls.make_user("AUTH0004")

    @classmethod
    def make_user(cls, username, group=None, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, full_name="인증 검증용 가상 사용자",
            password=cls.password, **kwargs,
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.csrf_url = reverse("accounts:csrf")
        self.login_url = reverse("accounts:login")
        self.me_url = reverse("accounts:me")
        self.logout_url = reverse("accounts:logout")

    def csrf_token(self, client=None):
        client = client or self.client
        response = client.get(self.csrf_url)
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def post_login(self, username=None, password=None, client=None, **request_headers):
        client = client or self.client
        return client.post(
            self.login_url,
            data=json.dumps({
                "username": username or self.user.username,
                "password": self.password if password is None else password,
            }),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token(client),
            **request_headers,
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertNotIn(self.password, response.content.decode())

    def test_csrf_bootstrap_is_anonymous_and_does_not_create_login_session(self):
        response = self.client.get(self.csrf_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["csrf_token"])
        self.assertTrue(response.cookies[settings.CSRF_COOKIE_NAME]["httponly"])
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertIn("no-store", response["Cache-Control"])

    def test_login_returns_only_public_identity_fields_and_fixed_expiry(self):
        started = timezone.now()
        with patch("django.utils.timezone.now", return_value=started):
            response = self.post_login()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data), {"user", "session_expires_at", "csrf_token"})
        self.assertEqual(data["user"], {
            "id": str(self.user.pk), "username": self.user.username,
            "full_name": self.user.full_name, "role": "user",
        })
        deadline = started + timedelta(hours=8)
        self.assertEqual(datetime.fromisoformat(data["session_expires_at"]), deadline)
        key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertEqual(Session.objects.get(pk=key).expire_date, deadline)
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))
        self.assertTrue(response.cookies[settings.SESSION_COOKIE_NAME]["httponly"])
        self.assertEqual(response.cookies[settings.SESSION_COOKIE_NAME]["samesite"], "Lax")
        self.assertIn("no-store", response["Cache-Control"])

    def test_service_admin_can_login_without_django_staff_flags(self):
        response = self.post_login(username=self.admin.username)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "admin")
        self.assertFalse(self.admin.is_staff)
        self.assertFalse(self.admin.is_superuser)

    def test_invalid_accounts_have_identical_error_and_no_session(self):
        expected = None
        for username, password in (
            ("MISSING1", self.password),
            (self.user.username, "WrongPass1!"),
            (self.inactive.username, self.password),
            (self.ungrouped.username, self.password),
        ):
            with self.subTest(username=username):
                response = self.post_login(username=username, password=password)
                self.assert_error(response, 401, "INVALID_CREDENTIALS")
                if expected is None:
                    expected = response.json()
                self.assertEqual(response.json(), expected)
                self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)

    def test_conflicting_groups_are_not_allowed_to_login(self):
        self.user.groups.add(self.admin_group)
        self.assert_error(self.post_login(), 401, "INVALID_CREDENTIALS")

    def test_staff_and_superuser_do_not_bypass_service_group_check(self):
        get_user_model().objects.filter(pk=self.ungrouped.pk).update(
            is_staff=True, is_superuser=True
        )
        response = self.post_login(username=self.ungrouped.username)
        self.assert_error(response, 401, "INVALID_CREDENTIALS")

    def test_login_requires_csrf_before_authentication(self):
        response = self.client.post(
            self.login_url,
            data=json.dumps({"username": self.user.username, "password": self.password}),
            content_type="application/json",
        )
        self.assert_error(response, 403, "CSRF_FAILED")
        self.assertFalse(Session.objects.exists())

    def test_untrusted_origin_is_rejected_even_with_csrf_token(self):
        response = self.client.post(
            self.login_url,
            data=json.dumps({"username": self.user.username, "password": self.password}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token(), HTTP_ORIGIN="https://untrusted.example",
        )
        self.assert_error(response, 403, "CSRF_FAILED")

    def test_login_does_not_trim_or_normalize_credentials(self):
        self.assert_error(self.post_login(username="auth0001"), 400, "INVALID_INPUT")
        self.assert_error(self.post_login(password=" " + self.password), 401, "INVALID_CREDENTIALS")

    def test_malformed_json_invalid_fields_and_media_type(self):
        token = self.csrf_token()
        for payload in (
            {}, [], None, {"username": self.user.username},
            {"username": 12345678, "password": self.password},
            {"username": self.user.username, "password": None},
            {"username": self.user.username, "password": self.password, "role": "admin"},
        ):
            with self.subTest(payload_type=type(payload).__name__):
                response = self.client.post(
                    self.login_url, data=json.dumps(payload), content_type="application/json",
                    HTTP_X_CSRFTOKEN=token,
                )
                self.assert_error(response, 400, "INVALID_INPUT")
        response = self.client.post(
            self.login_url, data="{", content_type="application/json", HTTP_X_CSRFTOKEN=token,
        )
        self.assert_error(response, 400, "INVALID_JSON")
        response = self.client.post(
            self.login_url, data="username=AUTH0001", content_type="text/plain",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assert_error(response, 415, "UNSUPPORTED_MEDIA_TYPE")

    def test_current_user_requires_login_and_uses_current_name(self):
        self.assert_error(self.client.get(self.me_url), 401, "AUTH_REQUIRED")
        self.assertEqual(self.post_login().status_code, 200)
        get_user_model().objects.filter(pk=self.user.pk).update(full_name="변경된 가상 이름")
        response = self.client.get(self.me_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["full_name"], "변경된 가상 이름")
        self.assertIn("no-store", response["Cache-Control"])

    def test_deactivation_and_role_removal_take_effect_on_next_request(self):
        self.assertEqual(self.post_login().status_code, 200)
        self.user.groups.clear()
        self.assert_error(self.client.get(self.me_url), 403, "FORBIDDEN")
        self.user.groups.add(self.user_group)
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.assert_error(self.client.get(self.me_url), 401, "AUTH_REQUIRED")

    def test_expiry_does_not_extend_and_rejects_saved_cookie_at_eight_hours(self):
        started = timezone.now()
        with patch("django.utils.timezone.now", return_value=started):
            response = self.post_login()
        deadline = started + timedelta(hours=8)
        expected_expiry = response.json()["session_expires_at"]
        key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        with patch("django.utils.timezone.now", return_value=deadline - timedelta(seconds=1)):
            response = self.client.get(self.me_url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["session_expires_at"], expected_expiry)
            self.csrf_token()
            # 다른 API가 세션 내용을 저장해도 최초 로그인 만료 시각을 유지해야 한다.
            session = self.client.session
            session["test_marker"] = True
            session.save()
            self.assertEqual(Session.objects.get(pk=key).expire_date, deadline)
        with patch("django.utils.timezone.now", return_value=deadline):
            self.assert_error(self.client.get(self.me_url), 401, "AUTH_REQUIRED")

    def test_login_rotates_anonymous_session_key_and_csrf_token(self):
        session = self.client.session
        session["test_marker"] = True
        session.save()
        old_key = session.session_key
        self.csrf_token()
        old_csrf_cookie = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        self.assertEqual(self.post_login().status_code, 200)
        self.assertNotEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value, old_key)
        self.assertFalse(Session.objects.filter(pk=old_key).exists())
        self.assertNotEqual(self.client.cookies[settings.CSRF_COOKIE_NAME].value, old_csrf_cookie)

    def test_logout_requires_new_csrf_token_and_invalidates_saved_session(self):
        old_token = self.csrf_token()
        response = self.post_login()
        new_token = response.json()["csrf_token"]
        key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assert_error(self.client.get(self.logout_url), 405, "METHOD_NOT_ALLOWED")
        self.assert_error(self.client.post(self.logout_url), 403, "CSRF_FAILED")
        self.assert_error(
            self.client.post(self.logout_url, HTTP_X_CSRFTOKEN=old_token), 403, "CSRF_FAILED"
        )
        self.assertEqual(self.client.get(self.me_url).status_code, 200)
        response = self.client.post(self.logout_url, HTTP_X_CSRFTOKEN=new_token)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertFalse(Session.objects.filter(pk=key).exists())
        self.client.cookies[settings.SESSION_COOKIE_NAME] = key
        self.assert_error(self.client.get(self.me_url), 401, "AUTH_REQUIRED")
        self.assertEqual(
            self.client.post(self.logout_url, HTTP_X_CSRFTOKEN=new_token).status_code, 204
        )

    def test_logout_does_not_end_other_browser_session(self):
        other = Client(enforce_csrf_checks=True)
        response = self.post_login()
        self.assertEqual(self.post_login(client=other).status_code, 200)
        self.assertEqual(self.client.post(
            self.logout_url, HTTP_X_CSRFTOKEN=response.json()["csrf_token"]
        ).status_code, 204)
        self.assertEqual(other.get(self.me_url).status_code, 200)

    def test_wrong_http_methods_return_json(self):
        self.assert_error(self.client.get(self.login_url), 405, "METHOD_NOT_ALLOWED")
        response = self.client.post(self.me_url, HTTP_X_CSRFTOKEN=self.csrf_token())
        self.assert_error(response, 405, "METHOD_NOT_ALLOWED")

    @override_settings(SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
    def test_https_cookie_flags(self):
        csrf_response = self.client.get(self.csrf_url, secure=True)
        response = self.client.post(
            self.login_url,
            data=json.dumps({"username": self.user.username, "password": self.password}),
            content_type="application/json", secure=True,
            HTTP_X_CSRFTOKEN=csrf_response.json()["csrf_token"],
            HTTP_ORIGIN="https://testserver",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.cookies[settings.SESSION_COOKIE_NAME]["secure"])
        self.assertTrue(response.cookies[settings.CSRF_COOKIE_NAME]["secure"])

    @override_settings(SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
    def test_https_proxy_login_session_and_logout(self):
        headers = {
            "HTTP_HOST": "testserver",
            "HTTP_X_FORWARDED_PROTO": "https",
            "HTTP_ORIGIN": "https://testserver",
        }
        response = self.post_login(**headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.wsgi_request.is_secure())
        self.assertTrue(response.cookies[settings.SESSION_COOKIE_NAME]["secure"])
        self.assertTrue(response.cookies[settings.CSRF_COOKIE_NAME]["secure"])
        self.assertEqual(self.client.get(self.me_url, **headers).status_code, 200)
        response = self.client.post(
            self.logout_url,
            HTTP_X_CSRFTOKEN=response.json()["csrf_token"],
            **headers,
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Session.objects.exists())

    def test_https_proxy_rejects_untrusted_and_http_origins(self):
        for origin in ("https://untrusted.example", "http://testserver"):
            with self.subTest(origin=origin):
                response = self.post_login(
                    HTTP_HOST="testserver", HTTP_X_FORWARDED_PROTO="https", HTTP_ORIGIN=origin,
                )
                self.assert_error(response, 403, "CSRF_FAILED")
        self.assertFalse(Session.objects.exists())

    def test_https_proxy_requires_matching_csrf_cookie_and_token(self):
        for failure in ("missing_cookie", "missing_token", "invalid_token", "wrong_token"):
            with self.subTest(failure=failure):
                client = Client(enforce_csrf_checks=True)
                token = self.csrf_token(client)
                headers = {
                    "HTTP_HOST": "testserver",
                    "HTTP_X_FORWARDED_PROTO": "https",
                    "HTTP_ORIGIN": "https://testserver",
                    "HTTP_X_CSRFTOKEN": token,
                }
                if failure == "missing_cookie":
                    del client.cookies[settings.CSRF_COOKIE_NAME]
                elif failure == "missing_token":
                    del headers["HTTP_X_CSRFTOKEN"]
                elif failure == "invalid_token":
                    headers["HTTP_X_CSRFTOKEN"] = "invalid"
                else:
                    headers["HTTP_X_CSRFTOKEN"] = "a" * 64
                response = client.post(
                    self.login_url,
                    data=json.dumps({"username": self.user.username, "password": self.password}),
                    content_type="application/json", **headers,
                )
                self.assert_error(response, 403, "CSRF_FAILED")
        self.assertFalse(Session.objects.exists())

    def test_http_proxy_does_not_validate_https_origin(self):
        response = self.post_login(
            HTTP_HOST="testserver", HTTP_X_FORWARDED_PROTO="http", HTTP_ORIGIN="https://testserver",
        )
        self.assert_error(response, 403, "CSRF_FAILED")
        self.assertFalse(Session.objects.exists())

    def test_plain_http_same_origin_login(self):
        response = self.post_login(HTTP_ORIGIN="http://testserver")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.wsgi_request.is_secure())
