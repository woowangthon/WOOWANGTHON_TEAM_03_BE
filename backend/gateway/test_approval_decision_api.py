"""가상 데이터로 본인 승인·거절, 기한, 중복 처리, CSRF와 상태 저장을 검증한다."""

import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.roles import ADMIN_GROUP_NAME, USER_GROUP_NAME

from .models import DetectionSpan, Request, SensitiveTerm


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ApprovalDecisionAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.member = cls.account("USR63201", cls.user_group)
        cls.other = cls.account("USR63202", cls.user_group)
        cls.admin = cls.account("ADM63201", cls.admin_group)

    @classmethod
    def account(cls, username, group):
        user = get_user_model().objects.create_user(
            username=username, full_name="승인 검증용 가상 사용자", password="SecurePass1!",
        )
        user.groups.add(group)
        return user

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.member)

    def make_request(self, **kwargs):
        data = {
            "user": self.member, "client_request_key": uuid4(),
            "instruction": "가상 의뢰", "input_text": "가상 원문",
            "state": Request.State.AWAITING_APPROVAL,
            "inspection_status": Request.InspectionStatus.COMPLETE,
            "processing_route": Request.ProcessingRoute.EXTERNAL,
            "approval_status": Request.ApprovalStatus.PENDING,
            "approval_expires_at": timezone.now() + timedelta(minutes=30),
            "delivery_status": Request.DeliveryStatus.NOT_SENT,
        }
        data.update(kwargs)
        return Request.objects.create(**data)

    def url(self, record):
        return reverse("gateway:request-approval-decision", kwargs={"request_id": record.pk})

    def csrf_headers(self):
        token = self.client.get(reverse("accounts:csrf")).json()["csrf_token"]
        return {"HTTP_X_CSRFTOKEN": token}

    def submit(self, record, decision="approved"):
        return self.client.post(
            self.url(record), json.dumps({"decision": decision}),
            content_type="application/json", **self.csrf_headers(),
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertNotIn("request", response.json())
        self.assertIn("no-store", response["Cache-Control"])

    def test_approval_records_decision_but_does_not_mark_external_send_or_answer(self):
        record = self.make_request()
        now = timezone.now()
        with patch("gateway.approval_decision.timezone.now", return_value=now):
            response = self.submit(record)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"request": {
            "request_id": str(record.pk), "state": "queued",
            "approval": {"status": "approved", "decided_at": now.isoformat(), "expires_at": record.approval_expires_at.isoformat()},
            "replayed": False,
        }})
        self.assertIn("no-store", response["Cache-Control"])
        record.refresh_from_db()
        self.assertEqual(record.decided_at, now)
        self.assertEqual(record.approval_status, "approved")
        self.assertEqual(record.delivery_status, "not_sent")
        self.assertIsNone(record.external_send_started_at)
        self.assertIsNone(record.answer_text)
        self.assertIsNone(record.finished_at)
        self.assertEqual(record.input_text, "가상 원문")

    def test_rejection_records_terminal_state_and_hides_preview(self):
        record = self.make_request()
        response = self.submit(record, "rejected")
        self.assertEqual(response.status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.approval_status, "rejected")
        self.assertEqual(record.finished_at, record.decided_at)
        self.assertEqual(record.delivery_status, "not_sent")
        preview = self.client.get(reverse("gateway:request-approval-preview", kwargs={"request_id": record.pk}))
        self.assert_error(preview, 409, "APPROVAL_NOT_AVAILABLE")

    def test_approval_hides_preview_and_zero_detection_still_needs_explicit_choice(self):
        record = self.make_request()
        self.assertEqual(DetectionSpan.objects.filter(request=record).count(), 0)
        self.assertEqual(self.submit(record).status_code, 200)
        preview = self.client.get(reverse("gateway:request-approval-preview", kwargs={"request_id": record.pk}))
        self.assert_error(preview, 409, "APPROVAL_NOT_AVAILABLE")

    def test_repeat_same_choice_returns_original_timestamp_without_writes(self):
        for choice in ("approved", "rejected"):
            with self.subTest(choice=choice):
                record = self.make_request()
                first = self.submit(record, choice)
                self.assertEqual(first.status_code, 200)
                before = Request.objects.values().get(pk=record.pk)
                second = self.submit(record, choice)
                self.assertEqual(second.status_code, 200)
                self.assertTrue(second.json()["request"]["replayed"])
                self.assertEqual(second.json()["request"]["approval"], first.json()["request"]["approval"])
                self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_opposite_choice_cannot_replace_a_decision(self):
        for first, second in (("approved", "rejected"), ("rejected", "approved")):
            with self.subTest(first=first):
                record = self.make_request()
                self.assertEqual(self.submit(record, first).status_code, 200)
                before = Request.objects.values().get(pk=record.pk)
                self.assert_error(self.submit(record, second), 409, "APPROVAL_ALREADY_DECIDED")
                self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_replayed_approval_after_completion_or_deadline_never_requeues(self):
        record = self.make_request()
        self.assertEqual(self.submit(record).status_code, 200)
        Request.objects.filter(pk=record.pk).update(
            state="completed", answer_text="가상 완성 답변", finished_at=timezone.now(),
            delivery_status="confirmed_sent", external_send_started_at=timezone.now(),
        )
        before = Request.objects.values().get(pk=record.pk)
        with patch("gateway.approval_decision.timezone.now", return_value=record.approval_expires_at + timedelta(seconds=1)):
            response = self.submit(record)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["request"]["state"], "completed")
        self.assertTrue(response.json()["request"]["replayed"])
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)
        self.assertNotIn("가상 완성 답변", response.content.decode())

    def test_deadline_boundary_persists_expired_state_without_user_decision(self):
        now = timezone.now()
        for delta in (-1, 0, 1):
            for choice in ("approved", "rejected"):
                with self.subTest(delta=delta, choice=choice):
                    record = self.make_request(approval_expires_at=now + timedelta(seconds=delta))
                    with patch("gateway.approval_decision.timezone.now", return_value=now):
                        response = self.submit(record, choice)
                    record.refresh_from_db()
                    if delta <= 0:
                        self.assert_error(response, 409, "APPROVAL_EXPIRED")
                        self.assertEqual(record.state, "expired")
                        self.assertEqual(record.approval_status, "expired")
                        self.assertEqual(record.finished_at, record.approval_expires_at)
                        self.assertIsNone(record.decided_at)
                        self.assert_error(self.submit(record, choice), 409, "APPROVAL_EXPIRED")
                    else:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(record.approval_status, choice)

    def test_expiry_during_validation_is_persisted(self):
        record = self.make_request()
        def finish_at_deadline(current):
            clock.return_value = record.approval_expires_at
            return []
        with patch("gateway.approval_decision.timezone.now", return_value=timezone.now()) as clock:
            with patch("gateway.approval_decision.get_preview_spans", side_effect=finish_at_deadline):
                response = self.submit(record)
        self.assert_error(response, 409, "APPROVAL_EXPIRED")
        record.refresh_from_db()
        self.assertEqual(record.state, "expired")

    def test_other_hidden_and_missing_requests_are_not_found(self):
        records = [self.make_request(user=self.other), self.make_request(hidden_at=timezone.now())]
        for record in records:
            before = Request.objects.values().get(pk=record.pk)
            self.assert_error(self.submit(record), 404, "REQUEST_NOT_FOUND")
            self.assertEqual(Request.objects.values().get(pk=record.pk), before)
        record = Request(request_id=uuid4())
        self.assert_error(self.submit(record), 404, "REQUEST_NOT_FOUND")

    def test_admin_is_denied_even_for_own_legacy_request(self):
        self.client.force_login(self.admin)
        for user in (self.member, self.admin):
            self.assert_error(self.submit(self.make_request(user=user)), 403, "FORBIDDEN")

    def test_anonymous_inactive_and_expired_sessions_are_denied(self):
        record = self.make_request()
        self.client.logout()
        self.assert_error(self.submit(record), 403, "AUTH_REQUIRED")
        self.client.force_login(self.member)
        session = self.client.session
        session.set_expiry(timezone.now() - timedelta(seconds=1))
        session.save()
        self.assert_error(self.submit(record), 403, "AUTH_REQUIRED")
        self.client.force_login(self.member)
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.assert_error(self.submit(record), 403, "AUTH_REQUIRED")

    def test_missing_or_conflicting_roles_are_denied(self):
        record = self.make_request()
        self.member.groups.clear()
        self.assert_error(self.submit(record), 403, "FORBIDDEN")
        self.member.groups.add(self.user_group, self.admin_group)
        self.assert_error(self.submit(record), 403, "FORBIDDEN")

    def test_csrf_is_required_and_failure_does_not_change_record(self):
        record = self.make_request()
        before = Request.objects.values().get(pk=record.pk)
        response = self.client.post(self.url(record), '{"decision":"approved"}', content_type="application/json")
        self.assert_error(response, 403, "CSRF_FAILED")
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_invalid_payload_query_and_content_type_do_not_mutate(self):
        record = self.make_request()
        before = Request.objects.values().get(pk=record.pk)
        payloads = [{}, [], None, {"decision": None}, {"decision": []}, {"decision": True},
                    {"decision": "approve"}, {"decision": " approved"},
                    {"decision": "approved", "user_id": str(self.other.pk)},
                    {"decision": "approved", "approval_expires_at": "2099-01-01"}]
        for data in payloads:
            with self.subTest(data=data):
                response = self.client.post(self.url(record), json.dumps(data), content_type="application/json", **self.csrf_headers())
                self.assert_error(response, 400, "INVALID_INPUT")
        response = self.client.post(self.url(record) + "?decision=approved", '{"decision":"approved"}', content_type="application/json", **self.csrf_headers())
        self.assert_error(response, 400, "INVALID_INPUT")
        response = self.client.post(self.url(record), "{", content_type="application/json", **self.csrf_headers())
        self.assert_error(response, 400, "INVALID_JSON")
        response = self.client.post(self.url(record), {"decision": "approved"}, **self.csrf_headers())
        self.assert_error(response, 415, "UNSUPPORTED_MEDIA_TYPE")
        for media_type in ("application/x-www-form-urlencoded", "text/plain"):
            with self.subTest(media_type=media_type):
                response = self.client.post(
                    self.url(record), "decision=approved", content_type=media_type, **self.csrf_headers(),
                )
                self.assert_error(response, 415, "UNSUPPORTED_MEDIA_TYPE")
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_json_with_charset_parameter_is_accepted(self):
        record = self.make_request()
        response = self.client.post(
            self.url(record), '{"decision":"approved"}',
            content_type="application/json; charset=utf-8", **self.csrf_headers(),
        )
        self.assertEqual(response.status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.approval_status, "approved")

    def test_invalid_state_inspection_route_or_delivery_is_denied_without_mutation(self):
        cases = [{"state": value} for value in Request.State.values if value != "awaiting_approval"]
        cases += [{"inspection_status": value} for value in ("pending", "degraded", "failed")]
        cases += [{"processing_route": value} for value in (None, "local", "blocked")]
        cases += [{"delivery_status": value} for value in ("unknown", "confirmed_sent")]
        cases += [{field: timezone.now()} for field in ("decided_at", "finished_at", "external_send_started_at")]
        cases += [{"approval_status": None}, {"approval_status": "approved"}]
        for fields in cases:
            with self.subTest(fields=fields):
                record = self.make_request(**fields)
                before = Request.objects.values().get(pk=record.pk)
                self.assert_error(self.submit(record), 409, "APPROVAL_NOT_AVAILABLE")
                self.assertEqual(Request.objects.values().get(pk=record.pk), before)

    def test_incomplete_preview_or_invalid_span_cannot_be_approved(self):
        for fields in ({"input_text": ""}, {"approval_expires_at": None}):
            record = self.make_request(**fields)
            self.assert_error(self.submit(record), 503, "PREVIEW_NOT_AVAILABLE")
            record.refresh_from_db()
            self.assertEqual(record.approval_status, "pending")
        record = self.make_request()
        term = SensitiveTerm.objects.create(value="가상", label="company")
        span = DetectionSpan.objects.create(request=record, sensitive_term=term, input_source="instruction", start_offset=0, end_offset=2)
        DetectionSpan.objects.filter(pk=span.pk).update(input_source="invalid")
        self.assert_error(self.submit(record), 503, "PREVIEW_NOT_AVAILABLE")
        record.refresh_from_db()
        self.assertEqual(record.approval_status, "pending")

    def test_get_does_not_record_a_decision(self):
        record = self.make_request()
        self.assert_error(self.client.get(self.url(record)), 405, "METHOD_NOT_ALLOWED")
        record.refresh_from_db()
        self.assertEqual(record.approval_status, "pending")

    def test_database_failure_rolls_back_and_does_not_disclose_details(self):
        record = self.make_request()
        before = Request.objects.values().get(pk=record.pk)
        original_save = Request.save
        def save_then_fail(instance, *args, **kwargs):
            original_save(instance, *args, **kwargs)
            raise DatabaseError("PRIVATE_DB_DETAIL")
        with patch.object(Request, "save", save_then_fail):
            response = self.submit(record)
        self.assert_error(response, 503, "APPROVAL_SAVE_FAILED")
        self.assertNotIn("PRIVATE_DB_DETAIL", response.content.decode())
        self.assertEqual(Request.objects.values().get(pk=record.pk), before)
