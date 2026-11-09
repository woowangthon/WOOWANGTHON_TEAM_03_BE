"""가상 계정·의뢰로 본인 이력의 권한·페이지 처리·응답 정보 제한을 검증한다."""

from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.roles import ADMIN_GROUP_NAME, USER_GROUP_NAME

from .models import Document, Request


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RequestListAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.member = cls.account("USR64001", cls.user_group)
        cls.other = cls.account("USR64002", cls.user_group)
        cls.admin = cls.account("ADM64001", cls.admin_group)
        cls.unassigned = cls.account("NOG64001")

    @classmethod
    def account(cls, username, group=None):
        user = get_user_model().objects.create_user(
            username=username, full_name="이력 검증용 가상 사용자", password="SecurePass1!",
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.member)
        self.url = reverse("gateway:request-intake")

    def make_request(self, user=None, **kwargs):
        return Request.objects.create(
            user=self.member if user is None else user, client_request_key=uuid4(), **kwargs,
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertIn("no-store", response["Cache-Control"])

    def test_list_contains_only_own_visible_requests_and_does_not_modify_them(self):
        own = self.make_request(instruction="본인 가상 의뢰")
        hidden = self.make_request(instruction="숨긴 가상 의뢰", hidden_at=timezone.now())
        other = self.make_request(user=self.other, instruction="타인 가상 의뢰")
        before = list(Request.objects.order_by("request_id").values())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual([row["request_id"] for row in body["results"]], [str(own.pk)])
        for record in (hidden, other):
            self.assertNotIn(str(record.pk), response.content.decode())
            self.assertNotIn(record.instruction, response.content.decode())
        self.assertEqual(list(Request.objects.order_by("request_id").values()), before)
        self.assertIn("no-store", response["Cache-Control"])

    def test_anonymous_and_non_user_roles_are_denied_before_query_validation(self):
        self.client.logout()
        self.assert_error(self.client.get(self.url, {"page": "bad"}), 403, "AUTH_REQUIRED")
        for actor in (self.admin, self.unassigned):
            with self.subTest(username=actor.username):
                self.make_request(user=actor)
                self.client.force_login(actor)
                self.assert_error(self.client.get(self.url, {"page": "bad"}), 403, "FORBIDDEN")
        self.client.force_login(self.member)
        self.member.groups.add(self.admin_group)
        self.assert_error(self.client.get(self.url), 403, "FORBIDDEN")
        self.member.groups.clear()
        self.assert_error(self.client.get(self.url), 403, "FORBIDDEN")

    def test_expired_or_deactivated_session_cannot_read_history(self):
        self.make_request(instruction="비활성 계정의 가상 의뢰")
        session = self.client.session
        session.set_expiry(timezone.now() - timedelta(seconds=1))
        session.save()
        self.assert_error(self.client.get(self.url), 403, "AUTH_REQUIRED")
        self.client.force_login(self.member)
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.assert_error(self.client.get(self.url), 403, "AUTH_REQUIRED")

    def test_newest_first_order_is_stable_for_equal_timestamps_and_paginated(self):
        older = self.make_request(request_id=UUID(int=3))
        lower = self.make_request(request_id=UUID(int=1))
        higher = self.make_request(request_id=UUID(int=2))
        now = timezone.now()
        Request.objects.filter(pk=older.pk).update(created_at=now - timedelta(days=1))
        Request.objects.filter(pk__in=[lower.pk, higher.pk]).update(created_at=now)
        for number, record in enumerate((higher, lower, older), start=1):
            response = self.client.get(self.url, {"page": number, "page_size": 1})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual((body["count"], body["page"], body["page_size"], body["total_pages"]),
                             (3, number, 1, 3))
            self.assertEqual(body["previous_page"], number - 1 if number > 1 else None)
            self.assertEqual(body["next_page"], number + 1 if number < 3 else None)
            self.assertEqual([row["request_id"] for row in body["results"]], [str(record.pk)])
        self.assert_error(self.client.get(self.url, {"page": 4, "page_size": 1}),
                          404, "PAGE_NOT_FOUND")

    def test_default_page_size_and_maximum_page_size(self):
        for _ in range(21):
            self.make_request()
        first = self.client.get(self.url).json()
        self.assertEqual((first["count"], first["page"], first["page_size"]), (21, 1, 20))
        self.assertEqual(len(first["results"]), 20)
        self.assertEqual(first["next_page"], 2)
        last = self.client.get(self.url, {"page": 2}).json()
        self.assertEqual(len(last["results"]), 1)
        self.assertIsNone(last["next_page"])
        maximum = self.client.get(self.url, {"page_size": 100})
        self.assertEqual(maximum.status_code, 200)
        self.assertEqual(len(maximum.json()["results"]), 21)

    def test_empty_history_is_an_empty_first_page(self):
        self.make_request(user=self.other)
        self.make_request(hidden_at=timezone.now())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "count": 0, "page": 1, "page_size": 20, "total_pages": 1,
            "next_page": None, "previous_page": None, "results": [],
        })
        self.assert_error(self.client.get(self.url, {"page": 2}), 404, "PAGE_NOT_FOUND")

    def test_invalid_unknown_and_duplicate_query_parameters_are_rejected(self):
        cases = [
            {"user_id": str(self.other.pk)}, {"hidden_at": "all"}, {"ordering": "created_at"},
            {"state": "completed"}, {"q": "test"}, {"page": "2147483648"},
            {"page_size": "101"}, {"page": ["1", "2"]}, {"page_size": ["20", "20"]},
        ]
        for key in ("page", "page_size"):
            cases.extend({key: value} for value in ("", "0", "-1", "1.0", "+1", " 1", "１", "x"))
        for params in cases:
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                self.assert_error(response, 400, "INVALID_INPUT")
                self.assertIn("fields", response.json()["error"])

    def test_list_returns_bounded_preview_and_pdf_name_without_internal_data(self):
        instruction = "가😀" * 150
        record = self.make_request(
            instruction=instruction, state=Request.State.COMPLETED,
            input_text="가상 검사 원문", answer_text="가상 완성 답변", finished_at=timezone.now(),
            intake_fingerprint="a" * 64, warning_codes=["FAKE_WARNING"],
            inspection_versions={"test": "fictional"},
        )
        Document.objects.create(
            request=record, original_filename="가상문서.pdf", storage_object_key="private/fictional.pdf",
            extracted_text="가상 PDF 본문", extraction_status=Document.ExtractionStatus.READY,
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [{
            "request_id": str(record.pk), "instruction_preview": instruction[:200],
            "state": "completed", "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(), "finished_at": record.finished_at.isoformat(),
            "document": {"original_filename": "가상문서.pdf"},
        }])
        for value in (record.input_text, record.answer_text, "가상 PDF 본문", "private/fictional.pdf"):
            self.assertNotIn(value, response.content.decode())

    def test_pdf_only_and_all_request_states_are_listed(self):
        records = [self.make_request(state=state) for state in Request.State.values]
        Document.objects.create(request=records[0], original_filename="가상첨부.pdf")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        self.assertEqual({row["state"] for row in rows}, set(Request.State.values))
        self.assertEqual(len(rows), len(records))
        for row in rows:
            self.assertEqual(row["instruction_preview"], "")
            self.assertIsNone(row["finished_at"])
            self.assertNotIn("answer_text", row)
            if row["request_id"] == str(records[0].pk):
                self.assertEqual(row["document"], {"original_filename": "가상첨부.pdf"})
            else:
                self.assertIsNone(row["document"])

    def test_database_failure_is_not_reported_as_empty_history(self):
        with patch("gateway.request_list.get_request_history", side_effect=DatabaseError("private DB details")):
            response = self.client.get(self.url)
        self.assert_error(response, 503, "REQUEST_LIST_FAILED")
        self.assertNotIn("private DB details", response.content.decode())
