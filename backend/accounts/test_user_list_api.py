"""가상 계정으로 목록·검색·페이지 처리와 재설정 API 연결을 검증한다."""

import json
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .roles import ADMIN_GROUP_NAME, USER_GROUP_NAME


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UserListAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "SecurePass1!"
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin = cls.make_account("ADM57001", cls.admin_group)
        # 생성 순서가 아닌 로그인 ID 순으로 정렬되는지 확인한다.
        cls.second = cls.make_account("USR57002", cls.user_group, full_name="홍길동")
        cls.first = cls.make_account("USR57001", cls.user_group, full_name="김하나")
        cls.inactive = cls.make_account(
            "USR57003", cls.user_group, full_name="이퇴사", is_active=False,
        )
        cls.unassigned = cls.make_account("NOG57001")
        cls.conflicting = cls.make_account("MIX57001", cls.user_group)
        cls.conflicting.groups.add(cls.admin_group)
        cls.staff = cls.make_account("STF57001", cls.user_group, is_staff=True)
        cls.superuser = cls.make_account("SUP57001", cls.user_group, is_superuser=True)

    @classmethod
    def make_account(cls, username, group=None, **kwargs):
        full_name = kwargs.pop("full_name", "목록 검증용 가상 사용자")
        user = get_user_model().objects.create_user(
            username=username, full_name=full_name, password=cls.password, **kwargs,
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        # 기존 발급 URL의 이름과 POST 동작을 유지하며 같은 경로에 GET을 추가한다.
        self.url = reverse("accounts_admin:user-create")

    def login_as(self, user, client=None, password=None):
        client = client or self.client
        token = client.get(reverse("accounts:csrf")).json()["csrf_token"]
        response = client.post(
            reverse("accounts:login"),
            data=json.dumps({
                "username": user.username,
                "password": self.password if password is None else password,
            }),
            content_type="application/json", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertNotIn(self.password, response.content.decode())

    def test_admin_gets_only_active_regular_users_and_public_fields(self):
        self.login_as(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "count": 2, "page": 1, "page_size": 20, "total_pages": 1,
            "next_page": None, "previous_page": None,
            "results": [
                {"id": str(user.pk), "username": user.username, "full_name": user.full_name,
                 "role": "user", "is_active": True}
                for user in (self.first, self.second)
            ],
        })
        for user in (self.first, self.second):
            self.assertNotIn(user.password, response.content.decode())
        self.assertNotIn(self.password, response.content.decode())
        self.assertIn("no-store", response["Cache-Control"])

    def test_anonymous_and_regular_user_are_denied_before_query_validation(self):
        self.assert_error(self.client.get(self.url, {"page": "bad"}), 403, "AUTH_REQUIRED")
        self.login_as(self.first)
        self.assert_error(self.client.get(self.url), 403, "FORBIDDEN")

    def test_staff_or_superuser_flag_does_not_grant_list_permission(self):
        for user in (self.staff, self.superuser):
            with self.subTest(username=user.username):
                self.login_as(user)
                self.assert_error(self.client.get(self.url), 403, "FORBIDDEN")

    def test_revoked_or_conflicting_admin_role_is_denied(self):
        self.login_as(self.admin)
        self.admin.groups.add(self.user_group)
        self.assert_error(self.client.get(self.url), 403, "FORBIDDEN")
        self.admin.groups.clear()
        self.assert_error(self.client.get(self.url), 403, "FORBIDDEN")

    def test_inactive_and_expired_admin_sessions_are_denied(self):
        self.login_as(self.admin)
        expiry = self.client.session.get_expiry_date()
        with patch("django.utils.timezone.now", return_value=expiry + timedelta(seconds=1)):
            self.assert_error(self.client.get(self.url), 403, "AUTH_REQUIRED")
        self.login_as(self.admin)
        get_user_model().objects.filter(pk=self.admin.pk).update(is_active=False)
        self.assert_error(self.client.get(self.url), 403, "AUTH_REQUIRED")

    def test_status_filters_include_inactive_users_only_when_requested(self):
        self.login_as(self.admin)
        for status, expected in (
            ("active", [self.first, self.second]),
            ("inactive", [self.inactive]),
            ("all", [self.first, self.second, self.inactive]),
        ):
            with self.subTest(status=status):
                response = self.client.get(self.url, {"status": status})
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["count"], len(expected))
                self.assertEqual([item["id"] for item in body["results"]],
                                 [str(user.pk) for user in expected])
                self.assertEqual([item["is_active"] for item in body["results"]],
                                 [user.is_active for user in expected])

    def test_search_matches_id_or_name_and_combines_with_status(self):
        self.login_as(self.admin)
        for query, status, expected in (
            ("usr57001", "active", [self.first]),
            (" 길동 ", "active", [self.second]),
            ("퇴사", "active", []),
            ("퇴사", "inactive", [self.inactive]),
            ("USR57", "all", [self.first, self.second, self.inactive]),
            ("없는이름", "all", []),
        ):
            with self.subTest(query=query, status=status):
                response = self.client.get(self.url, {"q": query, "status": status})
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["count"], len(expected))
                self.assertEqual([item["id"] for item in body["results"]],
                                 [str(user.pk) for user in expected])

    def test_search_treats_sql_wildcards_as_literal_characters(self):
        percent = self.make_account("USR57004", self.user_group, full_name="가상 100% 직원")
        underscore = self.make_account("USR57005", self.user_group, full_name="가상_직원")
        self.login_as(self.admin)
        for query, expected in (("%", percent), ("_", underscore)):
            response = self.client.get(self.url, {"q": query})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([item["id"] for item in response.json()["results"]], [str(expected.pk)])

    def test_unrelated_groups_do_not_duplicate_users(self):
        self.first.groups.add(Group.objects.create(name="unrelated_group"))
        self.login_as(self.admin)
        body = self.client.get(self.url).json()
        self.assertEqual(body["count"], 2)
        self.assertEqual([item["username"] for item in body["results"]], ["USR57001", "USR57002"])

    def test_pagination_uses_stable_id_order_and_navigation(self):
        self.login_as(self.admin)
        for number, expected, previous, following in (
            (1, self.first, None, 2), (2, self.second, 1, 3), (3, self.inactive, 2, None),
        ):
            response = self.client.get(self.url, {"status": "all", "page": number, "page_size": 1})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual((body["count"], body["page"], body["page_size"], body["total_pages"]),
                             (3, number, 1, 3))
            self.assertEqual((body["previous_page"], body["next_page"]), (previous, following))
            self.assertEqual([item["id"] for item in body["results"]], [str(expected.pk)])
        self.assert_error(self.client.get(self.url, {"status": "all", "page": 4, "page_size": 1}),
                          404, "PAGE_NOT_FOUND")

    def test_default_page_size_limits_results_and_maximum_is_accepted(self):
        for number in range(10, 31):
            self.make_account(f"USR57{number:03d}", self.user_group)
        self.login_as(self.admin)
        body = self.client.get(self.url).json()
        self.assertEqual(body["count"], 23)
        self.assertEqual(len(body["results"]), 20)
        self.assertEqual(body["next_page"], 2)
        last = self.client.get(self.url, {"page": 2}).json()
        self.assertEqual(len(last["results"]), 3)
        self.assertIsNone(last["next_page"])
        response = self.client.get(self.url, {"page_size": 100})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 23)

    def test_no_matches_return_empty_first_page_and_reject_later_pages(self):
        self.login_as(self.admin)
        response = self.client.get(self.url, {"q": "없는이름"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "count": 0, "page": 1, "page_size": 20, "total_pages": 1,
            "next_page": None, "previous_page": None, "results": [],
        })
        self.assert_error(self.client.get(self.url, {"q": "없는이름", "page": 2}),
                          404, "PAGE_NOT_FOUND")

    def test_blank_search_and_one_hundred_character_search_are_valid(self):
        self.login_as(self.admin)
        for query in ("", "   "):
            response = self.client.get(self.url, {"q": query})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["count"], 2)
        response = self.client.get(self.url, {"q": "가" * 100})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_invalid_unknown_or_duplicate_query_parameters_are_rejected(self):
        self.login_as(self.admin)
        for params in (
            {"page": "0"}, {"page": "-1"}, {"page": "1.0"}, {"page": "1.5"},
            {"page": "abc"}, {"page": ""}, {"page": "１"}, {"page": "2147483648"},
            {"page_size": "0"}, {"page_size": "101"}, {"page_size": "-1"},
            {"page_size": "1.0"}, {"page_size": ""}, {"status": "unknown"},
            {"status": ""}, {"q": "가" * 101}, {"role": "admin"}, {"ordering": "password"},
        ):
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                self.assert_error(response, 400, "INVALID_INPUT")
                self.assertIn("fields", response.json()["error"])
        for query in ("q=first&q=second", "status=active&status=all",
                      "page=1&page=2", "page_size=1&page_size=100"):
            self.assert_error(self.client.get(f"{self.url}?{query}"), 400, "INVALID_INPUT")

    def test_get_does_not_require_csrf_or_extend_session_or_change_accounts(self):
        self.login_as(self.admin)
        key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        expiry = Session.objects.get(pk=key).expire_date
        before = list(get_user_model().objects.order_by("username").values(
            "id", "username", "full_name", "password", "is_active", "last_login",
        ))
        del self.client.cookies[settings.CSRF_COOKIE_NAME]
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(Session.objects.get(pk=key).expire_date, expiry)
        self.assertEqual(before, list(get_user_model().objects.order_by("username").values(
            "id", "username", "full_name", "password", "is_active", "last_login",
        )))

    def test_list_uuid_can_be_used_for_existing_password_reset(self):
        token = self.login_as(self.admin)
        item = self.client.get(self.url, {"q": self.first.username}).json()["results"][0]
        url = reverse("accounts_admin:password-reset", kwargs={"user_id": item["id"]})
        response = self.client.post(
            url, data=json.dumps({"new_password": "ChangedPass9!"}),
            content_type="application/json", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 204)
        self.first.refresh_from_db()
        self.assertTrue(self.first.check_password("ChangedPass9!"))
        self.login_as(self.first, client=Client(enforce_csrf_checks=True), password="ChangedPass9!")
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_inactive_result_remains_ineligible_for_password_reset(self):
        token = self.login_as(self.admin)
        item = self.client.get(self.url, {"status": "inactive"}).json()["results"][0]
        url = reverse("accounts_admin:password-reset", kwargs={"user_id": item["id"]})
        response = self.client.post(
            url, data=json.dumps({"new_password": "ChangedPass9!"}),
            content_type="application/json", HTTP_X_CSRFTOKEN=token,
        )
        self.assert_error(response, 404, "USER_NOT_FOUND")
        self.inactive.refresh_from_db()
        self.assertTrue(self.inactive.check_password(self.password))

    def test_database_failure_is_not_returned_as_an_empty_success(self):
        self.login_as(self.admin)
        with patch("accounts.user_list.get_regular_users", side_effect=DatabaseError("private DB detail")):
            response = self.client.get(self.url)
        self.assert_error(response, 503, "USER_LIST_FAILED")
        self.assertNotIn("private DB detail", response.content.decode())
