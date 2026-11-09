"""가상 데이터로 통계 기준·미수집 구분·검색 일치·관리자 권한을 검증한다."""

from datetime import date, datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.roles import ADMIN_GROUP_NAME, USER_GROUP_NAME

from .models import DetectionSpan, Document, Request, SensitiveTerm


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AdminStatisticsAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin = cls.account("ADM65301", cls.admin_group)
        cls.member = cls.account("USR65301", cls.user_group, full_name="가상 김하나")
        cls.other = cls.account("USR65302", cls.user_group, full_name="가상 이둘")
        cls.staff = cls.account("STF65301", is_staff=True, is_superuser=True)

    @classmethod
    def account(cls, username, group=None, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, full_name=kwargs.pop("full_name", "통계 검증용 가상 사용자"),
            password="SecurePass1!", **kwargs,
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.admin)
        self.url = reverse("gateway_admin:statistics")
        self.period = {"date_from": "2026-11-09", "date_to": "2026-11-10"}

    def make_request(self, user=None, received_at=None, **kwargs):
        record = Request.objects.create(
            user=self.member if user is None else user, client_request_key=uuid4(), **kwargs,
        )
        Request.objects.filter(pk=record.pk).update(
            created_at=received_at or datetime(2026, 11, 9, 3, tzinfo=datetime_timezone.utc),
        )
        return record

    def add_span(self, record, label="company", start=0):
        term, _ = SensitiveTerm.objects.get_or_create(value=f"가상-{label}", label=label)
        return DetectionSpan.objects.create(
            request=record, sensitive_term=term, input_source=DetectionSpan.InputSource.INSTRUCTION,
            start_offset=start, end_offset=start + 2,
        )

    def fetch(self, **filters):
        response = self.client.get(self.url, {**self.period, **filters})
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertIn("no-store", response["Cache-Control"])
        return response.json()

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertIn("no-store", response["Cache-Control"])

    def assert_metric(self, item, name, known, unknown=0, status="complete"):
        self.assertEqual(item["metrics"][name], {
            "value": known if unknown == 0 else None, "known_value": known,
            "unknown_request_count": unknown, "status": status,
        })

    def test_six_metrics_count_requests_and_spans_without_multiplying_rows(self):
        blocked = self.make_request(
            state="blocked", inspection_status="complete", processing_route="blocked",
            warning_codes=["FAKE_WARNING_A", "FAKE_WARNING_B"],
        )
        self.add_span(blocked, "company", 0)
        self.add_span(blocked, "company", 10)
        self.add_span(blocked, "person", 20)
        sent = self.make_request(
            state="failed", inspection_status="complete", processing_route="external",
            delivery_status="confirmed_sent", error_stage="answer", error_code="FAKE_ANSWER_FAILURE",
        )
        self.add_span(sent, "person")
        self.make_request(state="rejected", inspection_status="complete", processing_route="external")
        self.make_request(state="expired", inspection_status="complete", processing_route="external")
        self.make_request(state="failed", inspection_status="degraded", warning_codes=["FAKE_WARNING_A"])
        self.make_request(state="failed", inspection_status="failed")
        self.make_request(state="analyzing")
        result = self.fetch()["summary"]
        self.assert_metric(result, "total_requests", 7)
        self.assert_metric(result, "detection_count", 4, 3, "partial")
        self.assert_metric(result, "warning_requests", 2, 2, "partial")
        self.assert_metric(result, "inspection_anomalies", 2, 1, "partial")
        self.assert_metric(result, "policy_blocked_requests", 1)
        self.assert_metric(result, "external_sent_confirmed_requests", 1)
        self.assertEqual(result["coverage"], {
            "inspection_complete_requests": 4, "inspection_pending_requests": 1,
            "inspection_degraded_requests": 1, "inspection_failed_requests": 1,
            "external_delivery_unknown_requests": 0,
        })

    def test_completed_inspection_with_no_findings_is_confirmed_zero(self):
        self.make_request(inspection_status="complete")
        summary = self.fetch()["summary"]
        for name in ("detection_count", "warning_requests", "inspection_anomalies"):
            self.assert_metric(summary, name, 0)

    def test_pending_results_are_unavailable_instead_of_zero(self):
        self.make_request()
        summary = self.fetch()["summary"]
        self.assert_metric(summary, "total_requests", 1)
        for name in ("detection_count", "warning_requests", "inspection_anomalies"):
            self.assert_metric(summary, name, 0, 1, "unavailable")

    def test_partial_spans_do_not_become_final_counts_and_warnings_count_requests(self):
        for status in ("pending", "degraded", "failed"):
            record = self.make_request(inspection_status=status, warning_codes=["FAKE_A", "FAKE_B", "FAKE_A"])
            self.add_span(record)
        summary = self.fetch()["summary"]
        self.assert_metric(summary, "detection_count", 0, 3, "unavailable")
        self.assert_metric(summary, "warning_requests", 3)
        self.assert_metric(summary, "inspection_anomalies", 2, 1, "partial")

    def test_answer_failure_after_successful_inspection_is_not_an_inspection_anomaly(self):
        self.make_request(state="failed", inspection_status="complete", error_stage="answer")
        summary = self.fetch()["summary"]
        self.assert_metric(summary, "inspection_anomalies", 0)
        self.assert_metric(summary, "policy_blocked_requests", 0)

    def test_external_confirmation_is_distinct_from_approval_route_and_unknown_delivery(self):
        self.make_request(processing_route="external", approval_status="approved", delivery_status="not_sent")
        self.make_request(processing_route="external", approval_status="pending")
        self.make_request(state="failed", delivery_status="confirmed_sent")
        self.make_request(state="completed", delivery_status="confirmed_sent")
        self.make_request(state="failed", delivery_status="unknown")
        self.make_request(state="processing", external_send_started_at=timezone.now())
        summary = self.fetch()["summary"]
        self.assert_metric(summary, "external_sent_confirmed_requests", 2)
        self.assertEqual(summary["coverage"]["external_delivery_unknown_requests"], 2)

    def test_daily_dates_use_seoul_received_time_and_fill_empty_days(self):
        instants = [
            datetime(2026, 11, 8, 14, 59, 59, 999999, tzinfo=datetime_timezone.utc),
            datetime(2026, 11, 8, 15, tzinfo=datetime_timezone.utc),
            datetime(2026, 11, 9, 14, 59, 59, 999999, tzinfo=datetime_timezone.utc),
            datetime(2026, 11, 9, 15, tzinfo=datetime_timezone.utc),
            datetime(2026, 11, 10, 15, tzinfo=datetime_timezone.utc),
        ]
        for instant in instants:
            self.make_request(received_at=instant, finished_at=instant + timedelta(days=20))
        body = self.fetch()
        self.assertEqual(body["timezone"], "Asia/Seoul")
        self.assertEqual(body["date_basis"], "created_at")
        self.assertIsNotNone(datetime.fromisoformat(body["calculated_at"]).tzinfo)
        self.assertEqual([(row["date"], row["metrics"]["total_requests"]["value"]) for row in body["daily"]],
                         [("2026-11-09", 2), ("2026-11-10", 1)])
        self.assert_metric(body["summary"], "total_requests", 3)
        extended = self.fetch(date_to="2026-11-12")
        self.assertEqual(extended["daily"][-1]["date"], "2026-11-12")
        self.assert_metric(extended["daily"][-1], "total_requests", 0)

    def test_summary_equals_daily_known_counts_and_coverage(self):
        for day, status in ((9, "complete"), (10, "degraded"), (10, "failed")):
            record = self.make_request(
                received_at=datetime(2026, 11, day, 3, tzinfo=datetime_timezone.utc),
                inspection_status=status, warning_codes=["FAKE_WARNING"],
            )
            self.add_span(record)
        body = self.fetch()
        for name, item in body["summary"]["metrics"].items():
            for field in ("known_value", "unknown_request_count"):
                self.assertEqual(item[field], sum(day["metrics"][name][field] for day in body["daily"]))
        for name, value in body["summary"]["coverage"].items():
            self.assertEqual(value, sum(day["coverage"][name] for day in body["daily"]))

    def test_label_filter_counts_all_spans_on_matching_requests_without_duplicates(self):
        first = self.make_request(inspection_status="complete")
        second = self.make_request(inspection_status="complete")
        partial = self.make_request(inspection_status="degraded")
        self.add_span(first, "company", 0)
        self.add_span(first, "company", 10)
        self.add_span(first, "person", 20)
        self.add_span(second, "person")
        self.add_span(partial, "company")
        summary = self.fetch(detection_label="company")["summary"]
        self.assert_metric(summary, "total_requests", 1)
        self.assert_metric(summary, "detection_count", 3)

    def test_statistics_filters_match_admin_list_filters(self):
        first = self.make_request(inspection_status="complete", state="completed", processing_route="external")
        second = self.make_request(user=self.other, inspection_status="complete", state="failed", processing_route="local")
        self.add_span(first, "company")
        self.add_span(second, "person")
        Document.objects.create(request=first, original_filename="가상찾기.pdf")
        cases = [
            {}, {"q": "usr65301"}, {"q": "가상찾기"}, {"q": "김하나"},
            {"user_id": str(self.other.pk)}, {"request_id": str(first.pk)},
            {"request_id": str(uuid4())}, {"state": "failed"}, {"processing_route": "external"},
            {"detection_label": "person"},
            {"q": "가상찾기", "state": "completed", "processing_route": "external",
             "user_id": str(self.member.pk), "detection_label": "company"},
        ]
        for filters in cases:
            with self.subTest(filters=filters):
                listing = self.client.get(reverse("gateway_admin:request-list"), {**self.period, **filters})
                self.assertEqual(listing.status_code, 200)
                self.assert_metric(self.fetch(**filters)["summary"], "total_requests", listing.json()["count"])

    def test_hidden_inactive_and_legacy_owner_records_are_counted_without_writes(self):
        self.make_request(hidden_at=timezone.now())
        self.make_request(user=self.other)
        self.make_request(user=self.admin)
        get_user_model().objects.filter(pk=self.other.pk).update(is_active=False)
        self.other.groups.clear()
        before = list(Request.objects.order_by("pk").values())
        for _ in range(2):
            self.assert_metric(self.fetch()["summary"], "total_requests", 3)
        self.assertEqual(list(Request.objects.order_by("pk").values()), before)

    def test_empty_period_has_confirmed_zero_metrics(self):
        body = self.fetch()
        self.assertEqual(len(body["daily"]), 2)
        for item in [body["summary"], *body["daily"]]:
            for name in item["metrics"]:
                self.assert_metric(item, name, 0)

    def test_dates_are_required_and_invalid_or_repeated_filters_are_rejected(self):
        for params in ({}, {"date_from": "2026-11-09"}, {"date_to": "2026-11-10"}):
            with self.subTest(params=params):
                self.assert_error(self.client.get(self.url, params), 400, "INVALID_INPUT")
        cases = [
            {"date_from": "2026-02-30"}, {"date_from": "2026-11-9"}, {"date_to": "2026-11-10T00:00:00"},
            {"date_from": "2026-11-11"}, {"page": "1"}, {"page_size": "20"}, {"timezone": "UTC"},
            {"state": "success"}, {"processing_route": "cloud"}, {"detection_label": "other"},
            {"user_id": "bad"}, {"request_id": "bad"}, {"q": "a" * 101},
        ]
        for key, value in (
            ("date_from", "2026-11-09"), ("date_to", "2026-11-10"), ("state", "completed"),
            ("processing_route", "local"), ("detection_label", "company"),
            ("user_id", str(self.member.pk)), ("request_id", str(uuid4())), ("q", "sample"),
        ):
            cases.append({key: [value, value]})
            if key != "q":
                cases.append({key: ""})
        for params in cases:
            with self.subTest(params=params):
                response = self.client.get(self.url, {**self.period, **params})
                self.assert_error(response, 400, "INVALID_INPUT")
                self.assertIn("fields", response.json()["error"])

    def test_period_accepts_366_days_and_rejects_367_days(self):
        start = date(2026, 1, 1)
        body = self.fetch(date_from=start.isoformat(), date_to=(start + timedelta(days=365)).isoformat())
        self.assertEqual(len(body["daily"]), 366)
        self.assert_error(self.client.get(self.url, {
            "date_from": start.isoformat(), "date_to": (start + timedelta(days=366)).isoformat(),
        }), 400, "INVALID_INPUT")

    def test_anonymous_general_user_and_django_staff_are_denied(self):
        self.client.logout()
        self.assert_error(self.client.get(self.url), 403, "AUTH_REQUIRED")
        for actor in (self.member, self.staff):
            with self.subTest(actor=actor.username):
                self.client.force_login(actor)
                self.assert_error(self.client.get(self.url), 403, "FORBIDDEN")

    def test_revoked_and_conflicting_admin_groups_are_denied(self):
        self.admin.groups.clear()
        self.assert_error(self.client.get(self.url, self.period), 403, "FORBIDDEN")
        self.admin.groups.add(self.admin_group, self.user_group)
        self.assert_error(self.client.get(self.url, self.period), 403, "FORBIDDEN")

    def test_expired_and_inactive_admin_sessions_are_denied(self):
        session = self.client.session
        session.set_expiry(timezone.now() - timedelta(seconds=1))
        session.save()
        self.assert_error(self.client.get(self.url, self.period), 403, "AUTH_REQUIRED")
        self.client.force_login(self.admin)
        get_user_model().objects.filter(pk=self.admin.pk).update(is_active=False)
        self.assert_error(self.client.get(self.url, self.period), 403, "AUTH_REQUIRED")

    def test_response_does_not_expose_individual_content_or_storage_details(self):
        record = self.make_request(
            instruction="가상 의뢰 원문", input_text="가상 검사 원문", answer_text="가상 AI 답변",
            state="completed", inspection_status="complete", warning_codes=["FAKE_PRIVATE_WARNING"],
        )
        Document.objects.create(request=record, original_filename="가상개별파일.pdf", storage_object_key="private/fake.pdf")
        self.add_span(record)
        body = self.fetch()
        self.assertEqual(set(body), {"timezone", "date_basis", "calculated_at", "filters", "summary", "daily"})
        self.assertEqual(body["filters"], {**self.period, "q": ""})
        self.assertEqual(set(body["summary"]["metrics"]), {
            "total_requests", "detection_count", "warning_requests", "inspection_anomalies",
            "policy_blocked_requests", "external_sent_confirmed_requests",
        })
        for value in (str(record.pk), self.member.username, self.member.password, record.instruction,
                      record.input_text, record.answer_text, "가상개별파일.pdf", "private/fake.pdf",
                      "가상-company", "FAKE_PRIVATE_WARNING"):
            self.assertNotIn(value, str(body))

    def test_database_failure_is_not_reported_as_zero(self):
        with patch("gateway.admin_statistics.get_filtered_admin_requests", side_effect=DatabaseError("private DB details")):
            response = self.client.get(self.url, self.period)
        self.assert_error(response, 503, "ADMIN_STATISTICS_FAILED")
        self.assertNotIn("summary", response.json())
        self.assertNotIn("daily", response.json())
        self.assertNotIn("private DB details", response.content.decode())

    def test_route_is_read_only_and_does_not_shadow_request_apis(self):
        self.assertEqual(self.url, "/api/v1/admin/statistics/")
        record = self.make_request()
        self.assertEqual(self.client.get(reverse("gateway_admin:request-list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("gateway_admin:request-detail", args=[record.pk])).status_code, 200)
        token = self.client.get(reverse("accounts:csrf")).json()["csrf_token"]
        response = self.client.post(self.url, "{}", content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assert_error(response, 405, "METHOD_NOT_ALLOWED")
        self.assertEqual(Request.objects.count(), 1)
