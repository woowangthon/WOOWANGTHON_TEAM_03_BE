"""PostgreSQL에서 숨김과 승인·결과 저장의 동시 실행을 검증한다. 실제 AI 호출은 없다."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from accounts.roles import USER_GROUP_NAME

from .approval_decision import decide_approval
from .intake_validation import IntakeError
from .models import Request
from .request_visibility import hide_request


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RequestHideConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL의 행 잠금으로 검증합니다.")
        self.user = get_user_model().objects.create_user(
            username="USR72101", full_name="동시 숨김 검증용 가상 사용자", password="SecurePass1!",
        )
        self.user.groups.add(Group.objects.create(name=USER_GROUP_NAME))
        self.record = Request.objects.create(
            user=self.user, client_request_key=uuid4(), instruction="가상 의뢰", input_text="가상 의뢰",
            state="awaiting_approval", inspection_status="complete", processing_route="external",
            approval_status="pending", approval_expires_at=timezone.now() + timedelta(minutes=30),
        )

    def run_parallel(self, actions):
        barrier = Barrier(2)
        def execute(action):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                barrier.wait(timeout=10)
                try:
                    return action(user)
                except IntakeError as exc:
                    return {"error": exc.code}
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(execute, actions))

    def test_parallel_hides_keep_one_original_timestamp(self):
        results = self.run_parallel([lambda user: hide_request(user, self.record.pk)] * 2)
        self.assertEqual(results[0], results[1])
        self.record.refresh_from_db()
        self.assertEqual(self.record.hidden_at, results[0])
        self.assertEqual(self.record.state, "awaiting_approval")
        self.assertEqual(self.record.approval_status, "pending")
        self.assertEqual(Request.objects.count(), 1)

    def test_hide_and_approval_follow_lock_order_without_revoking_a_decision(self):
        results = self.run_parallel([
            lambda user: hide_request(user, self.record.pk),
            lambda user: decide_approval(user, self.record.pk, "approved"),
        ])
        self.record.refresh_from_db()
        self.assertIsNotNone(self.record.hidden_at)
        if "error" in results[1]:
            self.assertEqual(results[1]["error"], "REQUEST_NOT_FOUND")
            self.assertEqual(self.record.approval_status, "pending")
            self.assertEqual(self.record.state, "awaiting_approval")
            self.assertIsNone(self.record.decided_at)
        else:
            self.assertEqual(results[1]["approval"]["status"], "approved")
            self.assertEqual(self.record.approval_status, "approved")
            self.assertEqual(self.record.state, "queued")
            self.assertIsNotNone(self.record.decided_at)
        self.assertIsNone(self.record.external_send_started_at)

    def test_hide_does_not_overwrite_concurrent_completion(self):
        Request.objects.filter(pk=self.record.pk).update(
            state="processing", approval_status="approved", decided_at=timezone.now(),
        )
        def complete_request(user):
            # AI 저장자가 같은 부모 행 잠금과 관련 필드만 갱신한다는 연결 계약을 검증한다.
            with transaction.atomic():
                record = Request.objects.select_for_update().get(pk=self.record.pk)
                record.state = Request.State.COMPLETED
                record.answer_text = "가상 최종 답변"
                record.finished_at = timezone.now()
                record.save(update_fields=["state", "answer_text", "finished_at", "updated_at"])
            return "completed"
        results = self.run_parallel([lambda user: hide_request(user, self.record.pk), complete_request])
        self.assertEqual(results[1], "completed")
        self.record.refresh_from_db()
        self.assertIsNotNone(self.record.hidden_at)
        self.assertEqual(self.record.state, "completed")
        self.assertEqual(self.record.answer_text, "가상 최종 답변")
        self.assertIsNotNone(self.record.finished_at)
