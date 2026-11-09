"""실제 DB 연결 두 개로 동일 접수 키의 경쟁 상태를 검증한다."""

from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from threading import Barrier
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings

from accounts.roles import USER_GROUP_NAME

from .intake_service import create_intake
from .intake_validation import IntakeError, validate_intake
from .models import Document, Request
from .test_intake_api import pdf_upload


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class IntakeConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("운영 대상 PostgreSQL의 행 잠금으로 검증합니다.")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        setting = override_settings(PRIVATE_DOCUMENT_ROOT=self.tmp.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.user = get_user_model().objects.create_user(
            username="USR62009", full_name="동시 접수 검증용 가상 사용자", password="SecurePass1!",
        )
        self.user.groups.add(Group.objects.create(name=USER_GROUP_NAME))

    def run_parallel(self, inputs):
        barrier = Barrier(2)

        def submit(values):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                barrier.wait(timeout=10)
                try:
                    record, document, created = create_intake(user, *values)
                    return (str(record.pk), created)
                except IntakeError as exc:
                    return (exc.code, False)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, inputs))
        return results

    def test_parallel_pdf_intake_saves_one_request_and_one_document(self):
        values = validate_intake({"client_request_key": str(uuid4()), "pdf": pdf_upload()})
        results = self.run_parallel([values, values])
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(sorted(created for _, created in results), [False, True])
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(Document.objects.count(), 1)

    def test_parallel_different_input_with_same_key_returns_conflict(self):
        key = str(uuid4())
        inputs = [validate_intake({"client_request_key": key, "instruction": text}) for text in ("첫 요청", "다른 요청")]
        results = self.run_parallel(inputs)
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertIn(("REQUEST_KEY_CONFLICT", False), results)
        self.assertEqual(Request.objects.count(), 1)
