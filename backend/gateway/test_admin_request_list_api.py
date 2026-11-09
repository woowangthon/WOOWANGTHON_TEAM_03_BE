"""가상 의뢰로 관리자 검색·페이지·탐지 요약·정보 제한을 검증한다."""

from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.roles import ADMIN_GROUP_NAME, USER_GROUP_NAME

from .models import DetectionSpan, Document, Request, SensitiveTerm


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AdminRequestListAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin = cls.account("ADM65001", cls.admin_group)
        cls.member = cls.account("USR65001", cls.user_group, full_name="가상 김하나")
        cls.other = cls.account("USR65002", cls.user_group, full_name="가상 이둘")
        cls.staff = cls.account("STF65001", is_staff=True, is_superuser=True)

    @classmethod
    def account(cls, username, group=None, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, full_name=kwargs.pop("full_name", "관리 목록 검증용 가상 사용자"),
            password="SecurePass1!", **kwargs,
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.admin)
        self.url = reverse("gateway_admin:request-list")

    def make_request(self, user=None, **kwargs):
        return Request.objects.create(
            user=self.member if user is None else user, client_request_key=uuid4(), **kwargs,
        )

    def add_span(self, record, label, start=0):
        term, _ = SensitiveTerm.objects.get_or_create(value=f"가상-{label}", label=label)
        return DetectionSpan.objects.create(
            request=record, sensitive_term=term, input_source=DetectionSpan.InputSource.INSTRUCTION,
            start_offset=start, end_offset=start + 2,
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertIn("no-store", response["Cache-Control"])

    def assert_ids(self, params, expected):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.content[:500])
        body = response.json()
        self.assertEqual(body["count"], len(expected))
        self.assertEqual({row["request_id"] for row in body["results"]}, {str(row.pk) for row in expected})
        return body

    def test_admin_sees_hidden_inactive_and_legacy_owner_records_without_mutation(self):
        visible = self.make_request()
        hidden = self.make_request(hidden_at=timezone.now())
        inactive = self.make_request(user=self.other)
        legacy = self.make_request(user=self.admin)
        get_user_model().objects.filter(pk=self.other.pk).update(is_active=False)
        self.other.groups.clear()
        before = list(Request.objects.order_by("request_id").values())
        self.assert_ids({}, [visible, hidden, inactive, legacy])
        self.assertEqual(list(Request.objects.order_by("request_id").values()), before)

    def test_response_contains_current_user_metadata_but_no_answer_or_original_text(self):
        record = self.make_request(
            instruction="가상 의뢰 원문", input_text="가상 검사 원문", answer_text="가상 AI 답변",
            state=Request.State.COMPLETED, processing_route=Request.ProcessingRoute.EXTERNAL,
            inspection_status=Request.InspectionStatus.COMPLETE, finished_at=timezone.now(),
            warning_codes=["FAKE_WARNING"], error_code="FAKE_CODE", intake_fingerprint="a" * 64,
            inspection_versions={"private": "fictional"},
        )
        Document.objects.create(
            request=record, original_filename="가상자료.pdf", storage_object_key="private/fake.pdf",
            extracted_text="가상 PDF 원문", extraction_status=Document.ExtractionStatus.READY,
        )
        self.add_span(record, SensitiveTerm.Label.COMPANY)
        get_user_model().objects.filter(pk=self.member.pk).update(full_name="변경한 가상 이름")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [{
            "request_id": str(record.pk),
            "user": {"id": str(self.member.pk), "username": self.member.username, "full_name": "변경한 가상 이름"},
            "created_at": record.created_at.isoformat(), "finished_at": record.finished_at.isoformat(),
            "input_type": "pdf", "original_filename": "가상자료.pdf", "state": "completed",
            "processing_route": "external", "inspection_status": "complete",
            "detection_count": 1, "detection_labels": ["company"],
            "warning_codes": ["FAKE_WARNING"], "error_code": "FAKE_CODE",
        }])
        for value in (record.instruction, record.input_text, record.answer_text, "가상 PDF 원문",
                      "private/fake.pdf", self.member.password, "가상-company", "inspection_versions"):
            self.assertNotIn(value, response.content.decode())
        self.assertIn("no-store", response["Cache-Control"])

    def test_anonymous_user_and_django_operator_flags_do_not_grant_access(self):
        self.client.logout()
        self.assert_error(self.client.get(self.url, {"page": "invalid"}), 403, "AUTH_REQUIRED")
        for actor in (self.member, self.staff):
            with self.subTest(actor=actor.username):
                self.client.force_login(actor)
                self.assert_error(self.client.get(self.url, {"page": "invalid"}), 403, "FORBIDDEN")

    def test_revoked_or_conflicting_admin_groups_are_denied(self):
        self.admin.groups.clear()
        self.assert_error(self.client.get(self.url), 403, "FORBIDDEN")
        self.admin.groups.add(self.admin_group, self.user_group)
        self.assert_error(self.client.get(self.url), 403, "FORBIDDEN")

    def test_inactive_and_expired_admin_sessions_are_denied(self):
        session = self.client.session
        session.set_expiry(timezone.now() - timedelta(seconds=1))
        session.save()
        self.assert_error(self.client.get(self.url), 403, "AUTH_REQUIRED")
        self.client.force_login(self.admin)
        get_user_model().objects.filter(pk=self.admin.pk).update(is_active=False)
        self.assert_error(self.client.get(self.url), 403, "AUTH_REQUIRED")

    def test_user_and_request_id_filters_are_exact_and_combine_with_and(self):
        first = self.make_request()
        second = self.make_request(user=self.other)
        self.assert_ids({"user_id": str(self.member.pk)}, [first])
        self.assert_ids({"request_id": str(second.pk)}, [second])
        self.assert_ids({"request_id": str(second.pk), "user_id": str(self.member.pk)}, [])
        self.assert_ids({"request_id": str(uuid4())}, [])

    def test_text_search_matches_login_name_current_name_and_filename_only(self):
        first = self.make_request(instruction="검색제외본문")
        second = self.make_request(user=self.other)
        Document.objects.create(request=second, original_filename="Sample-가상문서.pdf")
        self.assert_ids({"q": "usr65001"}, [first])
        self.assert_ids({"q": "김하나"}, [first])
        self.assert_ids({"q": "sample"}, [second])
        self.assert_ids({"q": "검색제외본문"}, [])
        get_user_model().objects.filter(pk=self.member.pk).update(full_name="새로운가상이름")
        self.assert_ids({"q": "김하나"}, [])
        self.assert_ids({"q": "새로운가상이름"}, [first])

    def test_text_search_treats_sql_wildcards_as_literal_text(self):
        special = self.make_request()
        ordinary = self.make_request(user=self.other)
        Document.objects.create(request=special, original_filename="가상100%_검사.pdf")
        Document.objects.create(request=ordinary, original_filename="가상100AB검사.pdf")
        self.assert_ids({"q": "%_"}, [special])

    def test_dates_include_entire_seoul_day_and_use_received_time(self):
        instants = [
            datetime(2026, 11, 8, 14, 59, 59, 999999, tzinfo=datetime_timezone.utc),
            datetime(2026, 11, 8, 15, 0, tzinfo=datetime_timezone.utc),
            datetime(2026, 11, 9, 14, 59, 59, 999999, tzinfo=datetime_timezone.utc),
            datetime(2026, 11, 9, 15, 0, tzinfo=datetime_timezone.utc),
        ]
        records = []
        for instant in instants:
            record = self.make_request(finished_at=instants[-1] + timedelta(days=10))
            Request.objects.filter(pk=record.pk).update(created_at=instant)
            records.append(record)
        self.assert_ids({"date_from": "2026-11-09", "date_to": "2026-11-09"}, records[1:3])
        self.assert_ids({"date_from": "2026-11-09"}, records[1:])
        self.assert_ids({"date_to": "2026-11-09"}, records[:3])

    def test_state_and_route_filters_do_not_merge_distinct_outcomes(self):
        blocked = self.make_request(state="blocked", processing_route="blocked")
        rejected = self.make_request(state="rejected", processing_route="external")
        expired = self.make_request(state="expired", processing_route="external")
        failed = self.make_request(state="failed", processing_route="local")
        for state, expected in (("blocked", blocked), ("rejected", rejected), ("expired", expired), ("failed", failed)):
            with self.subTest(state=state):
                self.assert_ids({"state": state}, [expected])
        self.assert_ids({"processing_route": "external"}, [rejected, expired])
        self.assert_ids({"state": "failed", "processing_route": "external"}, [])

    def test_label_filter_does_not_duplicate_requests_or_shrink_their_summary(self):
        first = self.make_request(inspection_status="complete")
        second = self.make_request(inspection_status="complete")
        incomplete = self.make_request(inspection_status="degraded")
        self.add_span(first, "company", 0)
        self.add_span(first, "company", 10)
        self.add_span(first, "person", 20)
        self.add_span(second, "company", 0)
        self.add_span(incomplete, "company", 0)
        body = self.assert_ids({"detection_label": "company"}, [first, second])
        first_row = next(row for row in body["results"] if row["request_id"] == str(first.pk))
        self.assertEqual(first_row["detection_count"], 3)
        self.assertEqual(first_row["detection_labels"], ["company", "person"])
        pages = [self.client.get(self.url, {"detection_label": "company", "page_size": 1, "page": p}).json()
                 for p in (1, 2)]
        self.assertEqual([page["count"] for page in pages], [2, 2])
        self.assertEqual(len({page["results"][0]["request_id"] for page in pages}), 2)

    def test_unfinished_inspections_are_distinct_from_confirmed_zero_detections(self):
        records = {status: self.make_request(inspection_status=status) for status in Request.InspectionStatus.values}
        self.add_span(records["degraded"], "person")
        rows = self.client.get(self.url).json()["results"]
        for row in rows:
            if row["inspection_status"] == "complete":
                self.assertEqual(row["detection_count"], 0)
                self.assertEqual(row["detection_labels"], [])
            else:
                self.assertIsNone(row["detection_count"])
                self.assertIsNone(row["detection_labels"])
            self.assertEqual(row["input_type"], "text")
            self.assertIsNone(row["original_filename"])

    def test_all_filters_can_be_combined(self):
        wanted = self.make_request(inspection_status="complete", state="completed", processing_route="external")
        same_user = self.make_request(inspection_status="complete", state="failed", processing_route="external")
        other_user = self.make_request(user=self.other, inspection_status="complete", state="completed",
                                       processing_route="external")
        for record in (wanted, same_user, other_user):
            self.add_span(record, "company")
            Document.objects.create(request=record, original_filename="가상검색.pdf")
            Request.objects.filter(pk=record.pk).update(
                created_at=datetime(2026, 11, 9, 3, tzinfo=datetime_timezone.utc),
            )
        self.assert_ids({
            "user_id": str(self.member.pk), "state": "completed", "processing_route": "external",
            "detection_label": "company", "q": "가상검색", "date_from": "2026-11-09", "date_to": "2026-11-09",
        }, [wanted])

    def test_order_and_pagination_remain_stable_for_equal_timestamps(self):
        records = [self.make_request(request_id=UUID(int=i)) for i in (1, 2, 3)]
        now = timezone.now()
        Request.objects.filter(pk__in=[row.pk for row in records]).update(created_at=now)
        Request.objects.filter(pk=records[2].pk).update(created_at=now - timedelta(days=1))
        for page_number, record in enumerate((records[1], records[0], records[2]), start=1):
            response = self.client.get(self.url, {"page_size": 1, "page": page_number})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual((body["count"], body["page"], body["page_size"], body["total_pages"]),
                             (3, page_number, 1, 3))
            self.assertEqual([row["request_id"] for row in body["results"]], [str(record.pk)])
            self.assertEqual(body["previous_page"], page_number - 1 if page_number > 1 else None)
            self.assertEqual(body["next_page"], page_number + 1 if page_number < 3 else None)
        self.assert_error(self.client.get(self.url, {"page": 4, "page_size": 1}), 404, "PAGE_NOT_FOUND")

    def test_default_and_maximum_page_sizes(self):
        for _ in range(21):
            self.make_request()
        first = self.client.get(self.url).json()
        self.assertEqual((first["count"], first["page"], first["page_size"]), (21, 1, 20))
        self.assertEqual(len(first["results"]), 20)
        self.assertEqual(first["next_page"], 2)
        last = self.client.get(self.url, {"page": 2}).json()
        self.assertEqual(len(last["results"]), 1)
        self.assertIsNone(last["next_page"])
        self.assertEqual(len(self.client.get(self.url, {"page_size": 100}).json()["results"]), 21)

    def test_empty_search_returns_empty_first_page(self):
        self.make_request()
        response = self.client.get(self.url, {"q": "없는가상검색결과"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "count": 0, "page": 1, "page_size": 20, "total_pages": 1,
            "next_page": None, "previous_page": None, "results": [],
        })
        self.assert_error(self.client.get(self.url, {"q": "없는가상검색결과", "page": 2}), 404, "PAGE_NOT_FOUND")

    def test_invalid_unknown_empty_or_repeated_filters_are_rejected(self):
        cases = [
            {"ordering": "created_at"}, {"include_answer": "true"}, {"hidden_at": "all"},
            {"state": "success"}, {"processing_route": "cloud"}, {"detection_label": "unknown"},
            {"user_id": "bad"}, {"request_id": "bad"}, {"q": "a" * 101},
            {"date_from": "2026-02-30"}, {"date_to": "2026-11-9"},
            {"date_from": "2026-11-10T00:00:00"}, {"date_from": "2026-11-10", "date_to": "2026-11-09"},
            {"page_size": "101"}, {"page": "2147483648"},
        ]
        for key in ("page", "page_size"):
            cases.extend({key: value} for value in ("", "0", "-1", "1.0", "+1", " 1", "１", "bad"))
        for key in ("state", "processing_route", "detection_label", "user_id", "request_id", "date_from", "date_to"):
            cases.append({key: ""})
        for key, value in (("q", "test"), ("page", "1"), ("page_size", "20"), ("state", "completed"),
                           ("processing_route", "local"), ("detection_label", "company"),
                           ("user_id", str(self.member.pk)), ("request_id", str(uuid4())),
                           ("date_from", "2026-11-09"), ("date_to", "2026-11-09")):
            cases.append({key: [value, value]})
        for params in cases:
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                self.assert_error(response, 400, "INVALID_INPUT")
                self.assertIn("fields", response.json()["error"])

    def test_database_or_summary_failure_is_not_returned_as_empty_or_zero(self):
        self.make_request(inspection_status="complete")
        for target in ("gateway.admin_request_list.get_admin_request_history",
                       "gateway.admin_request_list.get_final_detection_summaries"):
            with self.subTest(target=target):
                with patch(target, side_effect=DatabaseError("private DB details")):
                    response = self.client.get(self.url)
                self.assert_error(response, 503, "ADMIN_REQUEST_LIST_FAILED")
                self.assertNotIn("private DB details", response.content.decode())
                self.assertNotIn("results", response.json())

    def test_route_does_not_shadow_account_list_and_rejects_writes(self):
        self.assertEqual(self.url, "/api/v1/admin/requests/")
        self.assertEqual(self.client.get(reverse("accounts_admin:user-create")).status_code, 200)
        token = self.client.get(reverse("accounts:csrf")).json()["csrf_token"]
        response = self.client.post(self.url, "{}", content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assert_error(response, 405, "METHOD_NOT_ALLOWED")
        self.assertEqual(Request.objects.count(), 0)
