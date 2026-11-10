"""가상 검사 데이터로 관리자 목록·통계의 경고/검사 상태 필터와 집계 일치를 검증한다."""

from datetime import datetime, timedelta, timezone as datetime_timezone
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.roles import ADMIN_GROUP_NAME, USER_GROUP_NAME

from .models import DetectionSpan, Document, Request, SensitiveTerm


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AdminInspectionFilterAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin = cls.account("ADM73001", cls.admin_group)
        cls.member = cls.account("USR73001", cls.user_group)
        cls.other = cls.account("USR73002", cls.user_group)
        cls.operator = cls.account("OPS73001", None, is_staff=True, is_superuser=True)

    @classmethod
    def account(cls, username, group, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, full_name="필터 검증용 가상 사용자", password="SecurePass1!", **kwargs,
        )
        if group is not None:
            user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.admin)
        self.list_url = reverse("gateway_admin:request-list")
        self.stats_url = reverse("gateway_admin:statistics")
        self.period = {"date_from": "2026-11-09", "date_to": "2026-11-10"}

    def make_request(self, received_at=None, **kwargs):
        values = {"user": self.member, "client_request_key": uuid4(), "instruction": "가상 의뢰"}
        values.update(kwargs)
        record = Request.objects.create(**values)
        Request.objects.filter(pk=record.pk).update(
            created_at=received_at or datetime(2026, 11, 9, 3, tzinfo=datetime_timezone.utc),
        )
        return record

    def add_span(self, record, label="company", start=0):
        term, _ = SensitiveTerm.objects.get_or_create(value=f"가상-{label}", label=label)
        return DetectionSpan.objects.create(
            request=record, sensitive_term=term, input_source="instruction", start_offset=start, end_offset=start + 2,
        )

    def assert_matching(self, filters, expected):
        params = {**self.period, **filters}
        listing = self.client.get(self.list_url, params)
        stats = self.client.get(self.stats_url, params)
        for response in (listing, stats):
            self.assertEqual(response.status_code, 200, response.content[:500])
            self.assertIn("no-store", response["Cache-Control"])
        body = listing.json()
        aggregate = stats.json()
        self.assertEqual(body["count"], len(expected))
        self.assertEqual({row["request_id"] for row in body["results"]}, {str(row.pk) for row in expected})
        self.assertEqual(aggregate["summary"]["metrics"]["total_requests"]["value"], len(expected))
        self.assertEqual(sum(day["metrics"]["total_requests"]["value"] for day in aggregate["daily"]), len(expected))
        return body, aggregate

    def assert_error(self, response, code="INVALID_INPUT", status=400):
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertIn("no-store", response["Cache-Control"])

    def test_warning_filter_uses_recorded_codes_not_detection_count(self):
        warned = [self.make_request(inspection_status=status, warning_codes=["FAKE_A", "FAKE_A", "FAKE_B"])
                  for status in Request.InspectionStatus.values]
        detected = self.make_request(inspection_status="complete")
        self.add_span(detected)
        self.make_request()
        _, stats = self.assert_matching({"has_warnings": "true"}, warned)
        self.assertEqual(stats["summary"]["metrics"]["warning_requests"]["value"], 4)

    def test_no_warning_means_confirmed_zero_and_excludes_unknown_results(self):
        clean = self.make_request(inspection_status="complete")
        answer_failure = self.make_request(inspection_status="complete", state="failed", error_stage="answer")
        self.make_request(inspection_status="complete", warning_codes=["FAKE_WARNING"])
        for status in ("pending", "degraded", "failed"):
            self.make_request(inspection_status=status)
        _, stats = self.assert_matching({"has_warnings": "false"}, [clean, answer_failure])
        self.assertEqual(stats["summary"]["metrics"]["warning_requests"], {
            "value": 0, "known_value": 0, "unknown_request_count": 0, "status": "complete",
        })

    def test_omitted_filters_keep_unknown_results_and_existing_statistics_semantics(self):
        clean = self.make_request(inspection_status="complete")
        unknown = self.make_request(inspection_status="pending")
        _, stats = self.assert_matching({}, [clean, unknown])
        self.assertEqual(stats["summary"]["metrics"]["warning_requests"]["status"], "partial")
        self.assertIsNone(stats["summary"]["metrics"]["warning_requests"]["value"])
        self.assertEqual(stats["filters"], {**self.period, "q": ""})

    def test_each_inspection_status_filters_both_endpoints(self):
        records = {status: self.make_request(inspection_status=status) for status in Request.InspectionStatus.values}
        for status, record in records.items():
            with self.subTest(status=status):
                self.assert_matching({"inspection_status": status}, [record])

    def test_anomalies_include_degraded_and_failed_but_not_answer_generation_failure(self):
        degraded = self.make_request(state="failed", inspection_status="degraded")
        failed = self.make_request(state="failed", inspection_status="failed")
        self.make_request(state="failed", inspection_status="complete", error_stage="answer")
        self.make_request(state="analyzing", inspection_status="pending")
        _, stats = self.assert_matching({"inspection_anomaly": "true"}, [degraded, failed])
        self.assertEqual(stats["summary"]["metrics"]["inspection_anomalies"]["value"], 2)

    def test_no_anomaly_requires_completed_inspection_even_if_answer_failed(self):
        normal = self.make_request(inspection_status="complete")
        answer_failure = self.make_request(state="failed", inspection_status="complete", error_stage="answer")
        for status in ("pending", "degraded", "failed"):
            self.make_request(inspection_status=status)
        self.assert_matching({"inspection_anomaly": "false"}, [normal, answer_failure])

    def test_request_outcome_is_distinct_from_inspection_state_and_warnings(self):
        records = {state: self.make_request(state=state, inspection_status="complete", warning_codes=["FAKE_WARNING"])
                   for state in ("blocked", "rejected", "expired", "failed", "completed")}
        for state, record in records.items():
            with self.subTest(state=state):
                _, stats = self.assert_matching({"state": state, "has_warnings": "true"}, [record])
                self.assertEqual(stats["summary"]["metrics"]["policy_blocked_requests"]["value"], 1 if state == "blocked" else 0)
        self.assert_matching({"inspection_anomaly": "true"}, [])

    def test_combined_filters_use_and_without_expanding_conflicting_conditions(self):
        wanted = self.make_request(inspection_status="degraded", warning_codes=["FAKE_WARNING"])
        self.make_request(inspection_status="failed", warning_codes=["FAKE_WARNING"])
        self.make_request(inspection_status="degraded")
        self.make_request(inspection_status="complete", warning_codes=["FAKE_WARNING"])
        self.assert_matching({"has_warnings": "true", "inspection_anomaly": "true", "inspection_status": "degraded"}, [wanted])
        for filters in (
            {"inspection_status": "complete", "inspection_anomaly": "true"},
            {"inspection_status": "pending", "inspection_anomaly": "false"},
            {"has_warnings": "false", "inspection_anomaly": "true"},
        ):
            with self.subTest(filters=filters):
                self.assert_matching(filters, [])

    def test_new_filters_combine_with_existing_user_dates_search_route_and_request_id(self):
        wanted = self.make_request(inspection_status="failed", state="failed", processing_route="external", warning_codes=["FAKE_WARNING"])
        Document.objects.create(request=wanted, original_filename="가상대상.pdf")
        self.make_request(user=self.other, inspection_status="failed", state="failed", processing_route="external", warning_codes=["FAKE_WARNING"])
        self.make_request(received_at=datetime(2026, 11, 8, 3, tzinfo=datetime_timezone.utc), inspection_status="failed", warning_codes=["FAKE_WARNING"])
        self.assert_matching({
            "q": "가상대상", "user_id": str(self.member.pk), "request_id": str(wanted.pk),
            "state": "failed", "processing_route": "external", "inspection_status": "failed",
            "has_warnings": "true", "inspection_anomaly": "true",
        }, [wanted])

    def test_label_filter_does_not_multiply_warning_or_request_counts(self):
        wanted = self.make_request(inspection_status="complete", warning_codes=["FAKE_A", "FAKE_B"])
        self.add_span(wanted, "company", 0)
        self.add_span(wanted, "company", 10)
        self.add_span(wanted, "person", 20)
        partial = self.make_request(inspection_status="degraded", warning_codes=["FAKE_A"])
        self.add_span(partial, "company")
        body, stats = self.assert_matching({"has_warnings": "true", "detection_label": "company"}, [wanted])
        self.assertEqual(body["results"][0]["detection_count"], 3)
        self.assertEqual(stats["summary"]["metrics"]["detection_count"]["value"], 3)
        self.assertEqual(stats["summary"]["metrics"]["warning_requests"]["value"], 1)
        # 기존 분류 검색은 최종 정상 검사 구간만 대상으로 한다.
        self.assert_matching({"inspection_anomaly": "true", "detection_label": "company"}, [])

    def test_statistics_cover_all_filtered_pages_not_only_current_page(self):
        records = [self.make_request(inspection_status="complete", warning_codes=["FAKE_WARNING"]) for _ in range(3)]
        self.make_request(inspection_status="complete")
        params = {**self.period, "has_warnings": "true"}
        found = []
        for page in (1, 2, 3):
            response = self.client.get(self.list_url, {**params, "page": page, "page_size": 1})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["count"], 3)
            self.assertEqual(body["total_pages"], 3)
            self.assertEqual(len(body["results"]), 1)
            found.append(body["results"][0]["request_id"])
        self.assertEqual(set(found), {str(row.pk) for row in records})
        stats = self.client.get(self.stats_url, params)
        self.assertEqual(stats.status_code, 200)
        self.assertEqual(stats.json()["summary"]["metrics"]["total_requests"]["value"], 3)

    def test_daily_counts_and_unknown_coverage_use_the_filtered_set(self):
        first = self.make_request(inspection_status="degraded", warning_codes=["FAKE_WARNING"])
        second = self.make_request(received_at=datetime(2026, 11, 10, 3, tzinfo=datetime_timezone.utc), inspection_status="failed")
        self.make_request(inspection_status="complete", warning_codes=["FAKE_WARNING"])
        _, stats = self.assert_matching({"inspection_anomaly": "true"}, [first, second])
        self.assertEqual([day["metrics"]["total_requests"]["value"] for day in stats["daily"]], [1, 1])
        self.assertEqual(stats["summary"]["metrics"]["warning_requests"], {
            "value": None, "known_value": 1, "unknown_request_count": 1, "status": "partial",
        })
        self.assertEqual(stats["summary"]["coverage"]["inspection_degraded_requests"], 1)
        self.assertEqual(stats["summary"]["coverage"]["inspection_failed_requests"], 1)

    def test_invalid_empty_and_repeated_filter_values_are_rejected(self):
        cases = []
        for key in ("has_warnings", "inspection_anomaly"):
            cases.extend({key: value} for value in ("", "1", "0", "True", "FALSE", "yes", " true", "false ", "all", "null"))
            cases.append({key: ["true", "true"]})
            cases.append({key: ["true", "false"]})
        cases.extend({"inspection_status": value} for value in ("", "success", "abnormal", "degraded,failed", "FAILED", " complete"))
        cases.append({"inspection_status": ["failed", "degraded"]})
        cases.append({"inspection_status": ["failed", "failed"]})
        for params in cases:
            for url in (self.list_url, self.stats_url):
                with self.subTest(params=params, url=url):
                    response = self.client.get(url, {**self.period, **params})
                    self.assert_error(response)
                    self.assertIn("fields", response.json()["error"])

    def test_filtered_audit_keeps_hidden_inactive_and_legacy_owner_records(self):
        records = [
            self.make_request(hidden_at=timezone.now(), inspection_status="failed", warning_codes=["FAKE_WARNING"]),
            self.make_request(user=self.other, inspection_status="degraded", warning_codes=["FAKE_WARNING"]),
            self.make_request(user=self.admin, inspection_status="failed", warning_codes=["FAKE_WARNING"]),
        ]
        get_user_model().objects.filter(pk=self.other.pk).update(is_active=False)
        self.other.groups.clear()
        self.assert_matching({"has_warnings": "true", "inspection_anomaly": "true"}, records)

    def test_filters_do_not_change_processing_data_or_expose_private_content(self):
        record = self.make_request(
            state="completed", inspection_status="complete", warning_codes=["FAKE_WARNING"],
            input_text="가상 검사 비밀 원문", answer_text="가상 비공개 답변",
        )
        before = Request.objects.values().get(pk=record.pk)
        body, stats = self.assert_matching({"has_warnings": "true", "inspection_anomaly": "false"}, [record])
        for content in (record.input_text, record.answer_text, self.member.password):
            self.assertNotIn(content, str(body))
            self.assertNotIn(content, str(stats))
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_statistics_echo_filters_without_new_defaults_or_boolean_conversion(self):
        filters = {"has_warnings": "false", "inspection_status": "complete", "inspection_anomaly": "false"}
        record = self.make_request(inspection_status="complete")
        _, stats = self.assert_matching(filters, [record])
        self.assertEqual(stats["filters"], {**self.period, "q": "", **filters})

    def test_filters_cannot_bypass_admin_permissions(self):
        params = {**self.period, "has_warnings": "true", "inspection_anomaly": "true"}
        self.client.logout()
        for url in (self.list_url, self.stats_url):
            self.assert_error(self.client.get(url, params), "AUTH_REQUIRED", 403)
        for user in (self.member, self.operator):
            self.client.force_login(user)
            for url in (self.list_url, self.stats_url):
                self.assert_error(self.client.get(url, params), "FORBIDDEN", 403)
        self.client.force_login(self.admin)
        self.admin.groups.add(self.user_group)
        for url in (self.list_url, self.stats_url):
            self.assert_error(self.client.get(url, params), "FORBIDDEN", 403)

    def test_expired_and_inactive_admin_cannot_query_filtered_data(self):
        params = {**self.period, "inspection_status": "failed"}
        session = self.client.session
        session.set_expiry(timezone.now() - timedelta(seconds=1))
        session.save()
        for url in (self.list_url, self.stats_url):
            self.assert_error(self.client.get(url, params), "AUTH_REQUIRED", 403)
        self.client.force_login(self.admin)
        get_user_model().objects.filter(pk=self.admin.pk).update(is_active=False)
        for url in (self.list_url, self.stats_url):
            self.assert_error(self.client.get(url, params), "AUTH_REQUIRED", 403)
