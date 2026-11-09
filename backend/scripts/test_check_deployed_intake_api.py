"""통신·DB 변경 없이 배포 확인 스크립트의 PDF·판정·후처리 경계를 검사한다."""

import contextlib
import io
import json
import unittest
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock
from uuid import uuid4

from pypdf import PdfReader

from check_deployed_intake_api import (
    ApiClient,
    CheckFailed,
    Scenario,
    make_pdf,
    multipart_pdf,
    verify,
    verify_pdf,
)


def response(status, data):
    return status, {"Content-Type": "application/json"}, json.dumps(data).encode()


class IntakeScriptTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_generated_pdf_is_valid_one_page_with_fictional_text(self):
        content = make_pdf()
        reader = PdfReader(io.BytesIO(content), strict=True)
        self.assertFalse(reader.is_encrypted)
        self.assertEqual(len(reader.pages), 1)
        self.assertIn("Fictional data only.", reader.pages[0].extract_text())

    def test_multipart_preserves_unicode_instruction_and_pdf_bytes(self):
        key, instruction, pdf = str(uuid4()), "가상 문서 확인 😀", make_pdf()
        body, content_type = multipart_pdf(key, instruction, "fictional.pdf", pdf)
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        parts = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
        self.assertEqual(set(parts), {"client_request_key", "instruction", "pdf"})
        self.assertEqual(parts["client_request_key"].get_payload(decode=True).decode(), key)
        self.assertEqual(parts["instruction"].get_payload(decode=True).decode(), instruction)
        self.assertEqual(parts["pdf"].get_filename(), "fictional.pdf")
        self.assertEqual(parts["pdf"].get_payload(decode=True), pdf)

    def test_permission_check_requires_expected_status_and_error_code(self):
        verify("차단", response(403, {"error": {"code": "FORBIDDEN"}}), 403, "FORBIDDEN")
        for result in (
            response(201, {"request": {}}),
            response(403, {"error": {"code": "CSRF_FAILED"}}),
            response(403, {"error": {"code": "AUTH_REQUIRED"}}),
        ):
            with self.subTest(status=result[0]), self.assertRaises(CheckFailed):
                verify("차단", result, 403, "FORBIDDEN")

    def test_pdf_check_rejects_wrong_content_even_with_http_200(self):
        pdf = make_pdf()
        verify_pdf("PDF", (200, {"Content-Type": "application/pdf"}, pdf), pdf)
        with self.assertRaises(CheckFailed):
            verify_pdf("PDF", (200, {"Content-Type": "application/pdf"}, b"different"), pdf)

    def test_conflicting_username_is_never_registered_for_deactivation(self):
        scenario = Scenario()
        scenario.admin = Mock()
        scenario.admin.post.return_value = response(409, {"error": {"code": "USERNAME_ALREADY_EXISTS"}})
        with self.assertRaises(CheckFailed):
            scenario.create_user("A")
        self.assertEqual(scenario.issued, [])
        self.assertEqual(scenario.pending_usernames, set())
        scenario.admin.post.reset_mock()
        self.assertTrue(scenario.cleanup())
        scenario.admin.post.assert_not_called()

    def test_uncertain_creation_is_reported_without_guessing_user_id(self):
        scenario = Scenario()
        scenario.admin = Mock()
        scenario.admin.post.side_effect = CheckFailed("통신 실패")
        with self.assertRaises(CheckFailed):
            scenario.create_user("A")
        self.assertEqual(scenario.issued, [])
        self.assertEqual(len(scenario.pending_usernames), 1)
        scenario.admin.post.reset_mock()
        self.assertFalse(scenario.cleanup())
        scenario.admin.post.assert_not_called()

    def test_cleanup_only_deactivates_recorded_users_and_keeps_documents(self):
        scenario = Scenario()
        scenario.admin = Mock()
        user_id = str(uuid4())
        scenario.issued = [{"id": user_id, "username": "T1234567"}]
        scenario.admin.post.return_value = response(200, {"user": {"id": user_id, "is_active": False}})
        self.assertTrue(scenario.cleanup())
        scenario.admin.post.assert_called_once_with(f"/api/v1/admin/users/{user_id}/deactivate/", {})
        scenario.admin.logout.assert_called_once()

    def test_bad_creation_response_does_not_deactivate_returned_id(self):
        scenario = Scenario()
        scenario.admin = Mock()
        scenario.admin.post.return_value = response(201, {
            "user": {"id": str(uuid4()), "username": "EXISTING", "role": "admin", "is_active": True},
        })
        with self.assertRaises(CheckFailed):
            scenario.create_user("A")
        self.assertEqual(scenario.issued, [])
        scenario.admin.post.reset_mock()
        self.assertFalse(scenario.cleanup())
        scenario.admin.post.assert_not_called()

    def test_transport_failure_does_not_retry_post_or_print_password(self):
        client = ApiClient()
        client.opener = Mock()
        client.opener.open.side_effect = OSError("internal detail must not be shown")
        with self.assertRaises(CheckFailed) as caught:
            client.call("POST", "/api/v1/auth/login/", data={"password": "not-a-real-password"})
        client.opener.open.assert_called_once()
        self.assertNotIn("not-a-real-password", str(caught.exception))
        self.assertNotIn("internal detail", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
