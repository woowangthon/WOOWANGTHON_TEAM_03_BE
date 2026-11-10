"""실제 PostgreSQL 연결로 준비·만료·이용자 결정 사이의 경쟁을 검증한다."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from .approval_decision import decide_approval
from .approval_lifecycle import expire_due_approvals, prepare_approval
from .intake_validation import IntakeError
from .models import Request


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ApprovalLifecycleConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL의 행 잠금으로 검증합니다.")
        self.user = get_user_model().objects.create_user(
            username="USR63501", full_name="기한 동시성 검증용 가상 사용자", password="SecurePass1!",
        )
        self.record = Request.objects.create(
            user=self.user, client_request_key=uuid4(), instruction="가상 의뢰", input_text="가상 의뢰",
            state="analyzing", inspection_status="complete", processing_route="external",
        )

    def run_parallel(self, actions):
        barrier = Barrier(2)
        def execute(action):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                barrier.wait(timeout=10)
                try:
                    return action()
                except IntakeError as exc:
                    return exc.code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(execute, actions))

    def test_parallel_preparation_sets_one_fixed_deadline(self):
        results = self.run_parallel([lambda: prepare_approval(self.record.pk)] * 2)
        self.assertEqual(sorted(created for _, created in results), [False, True])
        self.assertEqual(results[0][0].approval_expires_at, results[1][0].approval_expires_at)

    def test_parallel_expiry_commands_count_each_request_once(self):
        prepare_approval(self.record.pk)
        Request.objects.filter(pk=self.record.pk).update(approval_expires_at=timezone.now() - timedelta(seconds=1))
        results = self.run_parallel([expire_due_approvals, expire_due_approvals])
        self.assertEqual(sum(results), 1)
        self.record.refresh_from_db()
        self.assertEqual(self.record.state, "expired")

    def test_expiry_and_late_approval_cannot_approve_expired_request(self):
        prepare_approval(self.record.pk)
        Request.objects.filter(pk=self.record.pk).update(approval_expires_at=timezone.now() - timedelta(seconds=1))
        results = self.run_parallel([
            expire_due_approvals,
            lambda: decide_approval(get_user_model().objects.get(pk=self.user.pk), self.record.pk, "approved"),
        ])
        self.assertIn(results[0], (0, 1))
        self.assertEqual(results[1], "APPROVAL_EXPIRED")
        self.record.refresh_from_db()
        self.assertEqual(self.record.state, "expired")
        self.assertIsNone(self.record.decided_at)

    def test_expiry_skips_locked_approval_and_preserves_committed_decision(self):
        record, _ = prepare_approval(self.record.pk)
        locked = Event()
        release = Event()
        def approve_with_open_transaction():
            close_old_connections()
            try:
                with transaction.atomic():
                    result = decide_approval(get_user_model().objects.get(pk=self.user.pk), record.pk, "approved")
                    locked.set()
                    if not release.wait(timeout=10):
                        raise TimeoutError("가상 승인 트랜잭션 종료 신호가 없습니다.")
                return result
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(approve_with_open_transaction)
            try:
                self.assertTrue(locked.wait(timeout=10))
                with patch("gateway.approval_lifecycle.timezone.now", return_value=record.approval_expires_at):
                    self.assertEqual(expire_due_approvals(), 0)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=10)["approval"]["status"], "approved")
        with patch("gateway.approval_lifecycle.timezone.now", return_value=record.approval_expires_at):
            self.assertEqual(expire_due_approvals(), 0)
        self.record.refresh_from_db()
        self.assertEqual(self.record.approval_status, "approved")
        self.assertEqual(self.record.state, "queued")
