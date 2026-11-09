"""가상 PDF와 계정으로 접수·중복 방지·원본 권한·실패 시 정리를 검증한다."""

import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfWriter

from accounts.roles import ADMIN_GROUP_NAME, USER_GROUP_NAME

from .models import Document, Request
from .storage import document_storage


def pdf_bytes(pages=1, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if encrypted:
        writer.encrypt("fictional-only", algorithm="RC4-128")
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def pdf_upload(content=None, name="fictional.pdf", content_type="application/pdf"):
    return SimpleUploadedFile(name, pdf_bytes() if content is None else content, content_type)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RequestIntakeAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user_group = Group.objects.create(name=USER_GROUP_NAME)
        cls.admin_group = Group.objects.create(name=ADMIN_GROUP_NAME)
        cls.member = cls.account("USR62001", cls.user_group)
        cls.other = cls.account("USR62002", cls.user_group)
        cls.admin = cls.account("ADM62001", cls.admin_group)

    @classmethod
    def account(cls, username, group):
        user = get_user_model().objects.create_user(
            username=username, full_name="접수 검증용 가상 사용자", password="SecurePass1!",
        )
        user.groups.add(group)
        return user

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage_settings = override_settings(PRIVATE_DOCUMENT_ROOT=self.tmp.name)
        self.storage_settings.enable()
        self.addCleanup(self.storage_settings.disable)
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.member)
        self.url = reverse("gateway:request-intake")

    def headers(self):
        token = self.client.get(reverse("accounts:csrf")).json()["csrf_token"]
        return {"HTTP_X_CSRFTOKEN": token}

    def post_json(self, payload):
        return self.client.post(self.url, json.dumps(payload), content_type="application/json", **self.headers())

    def post_pdf(self, content=None, key=None, instruction=None):
        values = {"client_request_key": str(key or uuid4()), "pdf": pdf_upload(content)}
        if instruction is not None:
            values["instruction"] = instruction
        return self.client.post(self.url, values, **self.headers())

    def detail_url(self, request_id):
        return reverse("gateway:request-intake-detail", kwargs={"request_id": request_id})

    def file_url(self, request_id):
        return reverse("gateway:request-file", kwargs={"request_id": request_id})

    def update(self, request_id, payload):
        return self.client.patch(
            self.detail_url(request_id), json.dumps(payload), content_type="application/json", **self.headers(),
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertNotIn(self.tmp.name, response.content.decode())

    def stored_files(self):
        return list(Path(self.tmp.name).rglob("*.pdf"))

    def test_text_only_intake_has_owner_and_does_not_run_ai(self):
        key = str(uuid4())
        text = "  가상 질문\n이모지 😀  "
        response = self.post_json({"client_request_key": key, "instruction": text})
        self.assertEqual(response.status_code, 201)
        data = response.json()["request"]
        record = Request.objects.get(pk=data["request_id"])
        self.assertEqual(record.user_id, self.member.pk)
        self.assertEqual(data["client_request_key"], key)
        self.assertEqual(record.instruction, text)
        self.assertEqual(data["state"], "preparing")
        self.assertIsNone(data["document"])
        self.assertFalse(data["processing_available"])
        self.assertFalse(response.json()["replayed"])
        self.assertEqual(record.input_text, "")
        self.assertIsNone(record.processing_route)
        self.assertIsNone(record.answer_text)
        self.assertEqual(Document.objects.count(), 0)
        self.assertIn("no-store", response["Cache-Control"])

    def test_pdf_only_is_saved_privately_and_waits_for_extraction(self):
        content = pdf_bytes()
        response = self.post_pdf(content)
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["request"]
        record = Request.objects.get(pk=data["request_id"])
        document = record.document
        self.assertEqual(record.instruction, "")
        self.assertEqual(record.state, "preparing")
        self.assertEqual(document.extraction_status, "pending")
        self.assertIsNone(document.extracted_text)
        self.assertEqual(document.size_bytes, len(content))
        self.assertEqual(document.page_count, 1)
        self.assertEqual(document.original_filename, "fictional.pdf")
        self.assertNotIn("fictional", document.storage_object_key)
        self.assertEqual(len(self.stored_files()), 1)
        self.assertEqual(self.stored_files()[0].read_bytes(), content)
        self.assertEqual(self.stored_files()[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn("storage_object_key", response.content.decode())
        self.assertNotIn("intake_fingerprint", response.content.decode())
        with self.assertRaises(NotImplementedError):
            document_storage().url(document.storage_object_key)

    def test_pdf_and_instruction_are_accepted_in_one_intake(self):
        response = self.post_pdf(instruction="가상 문서를 요약해 주세요.")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Request.objects.get().instruction, "가상 문서를 요약해 주세요.")

    def test_repeated_intake_returns_same_record_without_saving_again(self):
        content, key = pdf_bytes(), uuid4()
        first = self.post_pdf(content, key, "가상 요청")
        self.assertEqual(first.status_code, 201)
        with patch("gateway.storage.PrivateDocumentStorage.save", side_effect=AssertionError("unexpected save")):
            second = self.post_pdf(content, key, "가상 요청")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(first.json()["request"], second.json()["request"])
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(len(self.stored_files()), 1)

    def test_key_reuse_with_changed_instruction_pdf_or_filename_conflicts(self):
        content, key = pdf_bytes(), uuid4()
        self.assertEqual(self.post_pdf(content, key, "원래 요청").status_code, 201)
        self.assert_error(self.post_pdf(content, key, "다른 요청"), 409, "REQUEST_KEY_CONFLICT")
        self.assert_error(self.post_pdf(pdf_bytes(pages=2), key, "원래 요청"), 409, "REQUEST_KEY_CONFLICT")
        response = self.client.post(self.url, {
            "client_request_key": str(key), "instruction": "원래 요청",
            "pdf": pdf_upload(content, name="renamed.pdf"),
        }, **self.headers())
        self.assert_error(response, 409, "REQUEST_KEY_CONFLICT")
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(len(self.stored_files()), 1)

    def test_same_key_is_scoped_to_each_user_and_new_key_creates_new_document(self):
        content, key = pdf_bytes(), uuid4()
        first = self.post_pdf(content, key).json()["request"]["request_id"]
        self.client.force_login(self.other)
        second = self.post_pdf(content, key)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first, second.json()["request"]["request_id"])
        self.assertEqual(self.post_pdf(content).status_code, 201)
        self.assertEqual(Document.objects.count(), 3)
        self.assertEqual(len(self.stored_files()), 3)

    def test_instruction_update_preserves_id_file_and_original_retry_fingerprint(self):
        content, key = pdf_bytes(), uuid4()
        created = self.post_pdf(content, key).json()["request"]
        request_id = created["request_id"]
        before = Request.objects.get(pk=request_id)
        response = self.update(request_id, {"instruction": "추가한 의뢰문"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["request"]["request_id"], request_id)
        record = Request.objects.get(pk=request_id)
        self.assertEqual(record.created_at, before.created_at)
        self.assertEqual(record.intake_fingerprint, before.intake_fingerprint)
        self.assertEqual(record.state, "preparing")
        replay = self.post_pdf(content, key)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["request"]["instruction"], "추가한 의뢰문")
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(len(self.stored_files()), 1)
        timestamp = record.updated_at
        self.assertEqual(self.update(request_id, {"instruction": record.instruction}).status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.updated_at, timestamp)

    def test_current_intake_details_exclude_ai_and_masking_content(self):
        record = Request.objects.create(
            user=self.member, client_request_key=uuid4(), instruction="의뢰문",
            input_text="검사 원문", state="completed", answer_text="최종 답변",
            warning_codes=["fictional-warning"], error_code="internal-only",
        )
        response = self.client.get(self.detail_url(record.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["request"]["state"], "completed")
        for field in ("input_text", "answer_text", "warning_codes", "error_code", "intake_fingerprint"):
            self.assertNotIn(field, response.json()["request"])

    def test_other_users_and_admin_cannot_read_or_edit_others_intake(self):
        request_id = self.post_pdf().json()["request"]["request_id"]
        for actor in (self.other, self.admin):
            self.client.force_login(actor)
            self.assert_error(self.client.get(self.detail_url(request_id)), 404, "REQUEST_NOT_FOUND")
            self.assert_error(self.update(request_id, {"instruction": "변경 시도"}), 404, "REQUEST_NOT_FOUND")
        self.assertEqual(Request.objects.get().instruction, "")

    def test_pdf_view_allows_owner_and_admin_but_not_other_user(self):
        content = pdf_bytes()
        request_id = self.post_pdf(content).json()["request"]["request_id"]
        for actor in (self.member, self.admin):
            self.client.force_login(actor)
            response = self.client.get(self.file_url(request_id), HTTP_ACCEPT="application/pdf")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertTrue(response["Content-Disposition"].startswith("inline;"))
            self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
            self.assertIn("no-store", response["Cache-Control"])
            # 테스트 클라이언트의 스트림 종료 처리가 파일을 닫도록 한다.
            # response.close()를 직접 호출하면 테스트 트랜잭션의 DB 연결까지 닫힐 수 있다.
            self.assertEqual(b"".join(response.streaming_content), content)
            self.assertTrue(response.closed)
            self.assertTrue(Request.objects.filter(pk=request_id).exists())
        self.client.force_login(self.other)
        self.assert_error(self.client.get(self.file_url(request_id), HTTP_ACCEPT="application/pdf"),
                          404, "DOCUMENT_NOT_FOUND")

    def test_hidden_request_is_not_returned_to_owner_but_admin_can_view_pdf(self):
        content, key = pdf_bytes(), uuid4()
        request_id = self.post_pdf(content, key).json()["request"]["request_id"]
        Request.objects.filter(pk=request_id).update(hidden_at=timezone.now())
        self.assert_error(self.client.get(self.detail_url(request_id)), 404, "REQUEST_NOT_FOUND")
        self.assert_error(self.client.get(self.file_url(request_id)), 404, "DOCUMENT_NOT_FOUND")
        self.assert_error(self.update(request_id, {"instruction": "변경"}), 404, "REQUEST_NOT_FOUND")
        self.assert_error(self.post_pdf(content, key), 409, "REQUEST_NOT_AVAILABLE")
        self.client.force_login(self.admin)
        response = self.client.get(self.file_url(request_id))
        self.assertEqual(response.status_code, 200)
        # 스트림을 소비하면 Django 테스트 클라이언트가 안전하게 응답을 종료한다.
        self.assertEqual(b"".join(response.streaming_content), content)
        self.assertTrue(response.closed)
        self.assertTrue(Request.objects.filter(pk=request_id).exists())

    def test_missing_request_or_file_has_safe_error(self):
        self.assert_error(self.client.get(self.detail_url(uuid4())), 404, "REQUEST_NOT_FOUND")
        self.assert_error(self.client.get(self.file_url(uuid4())), 404, "DOCUMENT_NOT_FOUND")
        request_id = self.post_pdf().json()["request"]["request_id"]
        self.stored_files()[0].unlink()
        self.assert_error(self.client.get(self.file_url(request_id)), 503, "DOCUMENT_UNAVAILABLE")

    def test_processing_or_terminal_requests_cannot_be_edited(self):
        record = Request.objects.create(user=self.member, client_request_key=uuid4(), instruction="원래 의뢰")
        for state in Request.State.values:
            if state == "preparing":
                continue
            Request.objects.filter(pk=record.pk).update(state=state)
            self.assert_error(self.update(record.pk, {"instruction": "변경"}), 409, "REQUEST_NOT_EDITABLE")
        record.refresh_from_db()
        self.assertEqual(record.instruction, "원래 의뢰")

    def test_failed_extraction_and_combined_character_limit_block_update(self):
        request_id = self.post_pdf().json()["request"]["request_id"]
        document = Document.objects.get(pk=request_id)
        document.extraction_status = "failed"
        document.save()
        self.assert_error(self.update(request_id, {"instruction": "변경"}), 409, "REQUEST_NOT_EDITABLE")
        document.extraction_status = "ready"
        document.extracted_text = "가" * 59_997
        document.save()
        self.assertEqual(self.update(request_id, {"instruction": "나"}).status_code, 200)
        self.assert_error(self.update(request_id, {"instruction": "나다"}), 400, "INPUT_TOO_LONG")

    def test_instruction_limit_counts_unicode_codepoints_without_normalizing(self):
        text = "😀" * 4_000
        self.assertEqual(self.post_json({"client_request_key": str(uuid4()), "instruction": text}).status_code, 201)
        for bad in (text + "가", "", " \n ", None, 1, [], "a\x00b", "\ud800"):
            self.assert_error(self.post_json({"client_request_key": str(uuid4()), "instruction": bad}),
                              400, "INVALID_INPUT")

    def test_input_requires_key_and_content_and_rejects_client_controlled_fields(self):
        for payload in (
            {}, {"instruction": "가상 질문"}, {"client_request_key": str(uuid4())},
            {"client_request_key": "not-a-uuid", "instruction": "질문"},
            {"client_request_key": 12, "instruction": "질문"}, [], None,
        ):
            self.assert_error(self.post_json(payload), 400, "INVALID_INPUT")
        for field in ("user_id", "state", "storage_object_key", "answer_text", "input_text", "intake_fingerprint"):
            payload = {"client_request_key": str(uuid4()), "instruction": "질문", field: "조작"}
            self.assert_error(self.post_json(payload), 400, "INVALID_INPUT")
        self.assertEqual(Request.objects.count(), 0)

    def test_pdf_structure_encryption_extension_and_page_limit(self):
        for content, code in (
            (b"not a PDF", "INVALID_PDF"), (b"%PDF-1.7\ncorrupt", "INVALID_PDF"),
            (pdf_bytes(pages=0), "INVALID_PDF"), (pdf_bytes(pages=21), "PDF_PAGE_LIMIT"),
            (pdf_bytes(encrypted=True), "PDF_ENCRYPTED"),
        ):
            self.assert_error(self.post_pdf(content), 400, code)
        self.assertEqual(self.post_pdf(pdf_bytes(pages=20)).status_code, 201)
        for name, mime in (("fake.txt", "application/pdf"), ("fake.pdf", "text/plain")):
            response = self.client.post(self.url, {
                "client_request_key": str(uuid4()), "pdf": pdf_upload(name=name, content_type=mime),
            }, **self.headers())
            self.assert_error(response, 400, "INVALID_PDF")
        self.assertEqual(Request.objects.count(), 1)

    def test_upload_limit_is_checked_during_streaming_and_accepts_boundary(self):
        content = pdf_bytes()
        with override_settings(PDF_MAX_BYTES=len(content)):
            self.assertEqual(self.post_pdf(content).status_code, 201)
        with override_settings(PDF_MAX_BYTES=len(content) - 1):
            self.assert_error(self.post_pdf(content), 413, "PDF_TOO_LARGE")
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(len(self.stored_files()), 1)

    def test_duplicate_fields_and_multiple_files_are_rejected(self):
        for data in (
            {"client_request_key": [str(uuid4()), str(uuid4())], "instruction": "질문"},
            {"client_request_key": str(uuid4()), "instruction": ["첫 번째", "두 번째"]},
            {"client_request_key": str(uuid4()), "pdf": [pdf_upload(), pdf_upload()]},
            {"client_request_key": str(uuid4()), "pdf": pdf_upload(), "other": pdf_upload()},
        ):
            response = self.client.post(self.url, data, **self.headers())
            self.assert_error(response, 400, "INVALID_INPUT")
        self.assertEqual(Request.objects.count(), 0)
        self.assertEqual(self.stored_files(), [])

    def test_update_rejects_empty_extra_or_non_string_input(self):
        request_id = self.post_pdf().json()["request"]["request_id"]
        for payload in ({}, {"instruction": " "}, {"instruction": None}, {"instruction": 1},
                        {"instruction": "정상", "pdf": "교체"}, []):
            self.assert_error(self.update(request_id, payload), 400, "INVALID_INPUT")

    def test_unauthenticated_inactive_and_invalid_role_users_are_denied(self):
        self.client.logout()
        self.assert_error(self.post_json({"client_request_key": str(uuid4()), "instruction": "질문"}),
                          403, "AUTH_REQUIRED")
        self.assert_error(self.client.get(self.file_url(uuid4())), 403, "AUTH_REQUIRED")
        self.client.force_login(self.member)
        self.member.groups.clear()
        self.assert_error(self.post_pdf(), 403, "FORBIDDEN")
        self.member.groups.add(self.user_group, self.admin_group)
        self.assert_error(self.post_pdf(), 403, "FORBIDDEN")
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.assert_error(self.post_pdf(), 403, "AUTH_REQUIRED")
        self.assertEqual(Request.objects.count(), 0)

    def test_csrf_method_media_and_json_rules(self):
        self.assert_error(self.client.post(self.url, "{}", content_type="application/json"), 403, "CSRF_FAILED")
        response = self.client.post(self.url, "{}", content_type="application/json",
                                    HTTP_ORIGIN="https://untrusted.example", **self.headers())
        self.assert_error(response, 403, "CSRF_FAILED")
        self.assert_error(self.client.get(self.url), 405, "METHOD_NOT_ALLOWED")
        self.assert_error(self.client.post(self.url, "{", content_type="application/json", **self.headers()),
                          400, "INVALID_JSON")
        self.assert_error(self.client.post(self.url, "text", content_type="text/plain", **self.headers()),
                          415, "UNSUPPORTED_MEDIA_TYPE")

    def test_large_json_is_rejected_before_deserialization(self):
        response = self.post_json({"client_request_key": str(uuid4()), "instruction": "a" * 65_536})
        self.assert_error(response, 413, "INPUT_TOO_LARGE")
        self.assertEqual(Request.objects.count(), 0)

    def test_database_failure_rolls_back_request_and_removes_saved_pdf(self):
        with patch.object(Document, "save", side_effect=DatabaseError("private DB details")):
            response = self.post_pdf()
        self.assert_error(response, 503, "INTAKE_SAVE_FAILED")
        self.assertNotIn("private DB details", response.content.decode())
        self.assertEqual(Request.objects.count(), 0)
        self.assertEqual(Document.objects.count(), 0)
        self.assertEqual(self.stored_files(), [])

    def test_storage_failure_rolls_back_request(self):
        with patch("gateway.storage.PrivateDocumentStorage.save", side_effect=OSError("private path")):
            response = self.post_pdf()
        self.assert_error(response, 503, "INTAKE_SAVE_FAILED")
        self.assertEqual(Request.objects.count(), 0)
        self.assertEqual(Document.objects.count(), 0)

    def test_instruction_save_failure_rolls_back(self):
        request_id = self.post_pdf().json()["request"]["request_id"]
        with patch.object(Request, "save", side_effect=DatabaseError("private DB details")):
            response = self.update(request_id, {"instruction": "변경"})
        self.assert_error(response, 503, "REQUEST_UPDATE_FAILED")
        self.assertEqual(Request.objects.get(pk=request_id).instruction, "")

    def test_legacy_key_without_fingerprint_is_not_guessed_as_replay(self):
        key = uuid4()
        Request.objects.create(user=self.member, client_request_key=key, instruction="가상 요청")
        self.assert_error(self.post_json({"client_request_key": str(key), "instruction": "가상 요청"}),
                          409, "REQUEST_KEY_CONFLICT")

    def test_intake_fingerprint_cannot_be_changed(self):
        self.assertEqual(self.post_json({"client_request_key": str(uuid4()), "instruction": "가상 요청"}).status_code, 201)
        record = Request.objects.get()
        record.intake_fingerprint = "a" * 64
        with self.assertRaises(ValidationError):
            record.save()
