"""PostgreSQL의 별도 연결로 승인·거절 경쟁과 중복 기록 방지를 검증한다."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from .approval_decision import decide_approval
from .intake_validation import IntakeError
from .models import Request


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ApprovalConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL의 행 잠금으로 검증합니다.")
        self.user = get_user_model().objects.create_user(
            username="USR63301", full_name="동시 승인 검증용 가상 사용자", password="SecurePass1!",
        )
        self.record = Request.objects.create(
            user=self.user, client_request_key=uuid4(), instruction="가상 의뢰", input_text="가상 원문",
            state="awaiting_approval", inspection_status="complete", processing_route="external",
            approval_status="pending", approval_expires_at=timezone.now() + timedelta(minutes=30),
        )

    def run_parallel(self, choices):
        barrier = Barrier(2)
        def submit(choice):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                # 회귀로 교착이 발생해도 테스트가 무한 대기하지 않도록 제한한다.
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                barrier.wait(timeout=10)
                try:
                    return decide_approval(user, self.record.pk, choice)
                except IntakeError as exc:
                    return {"error": exc.code}
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(submit, choices))

    def test_parallel_approvals_record_once_without_external_send(self):
        results = self.run_parallel(["approved", "approved"])
        self.assertEqual(sorted(item["replayed"] for item in results), [False, True])
        self.assertEqual(results[0]["approval"], results[1]["approval"])
        self.record.refresh_from_db()
        self.assertEqual(self.record.state, "queued")
        self.assertEqual(self.record.approval_status, "approved")
        self.assertIsNone(self.record.external_send_started_at)

    def test_parallel_approval_and_rejection_accept_only_one_choice(self):
        results = self.run_parallel(["approved", "rejected"])
        accepted = [result for result in results if "error" not in result]
        self.assertEqual(len(accepted), 1)
        self.assertIn({"error": "APPROVAL_ALREADY_DECIDED"}, results)
        self.record.refresh_from_db()
        self.assertEqual(self.record.approval_status, accepted[0]["approval"]["status"])
        self.assertEqual(self.record.state, "queued" if self.record.approval_status == "approved" else "rejected")

    def test_parallel_expired_decisions_persist_expiry(self):
        Request.objects.filter(pk=self.record.pk).update(approval_expires_at=timezone.now() - timedelta(seconds=1))
        results = self.run_parallel(["approved", "rejected"])
        self.assertEqual(results, [{"error": "APPROVAL_EXPIRED"}, {"error": "APPROVAL_EXPIRED"}])
        self.record.refresh_from_db()
        self.assertEqual(self.record.state, "expired")
        self.assertIsNone(self.record.decided_at)
