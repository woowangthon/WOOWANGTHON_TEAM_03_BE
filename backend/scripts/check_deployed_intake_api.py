"""실행 중인 서버에서 일반 사용자 발급·의뢰 접수·PDF 접근 권한을 확인한다.

--create-test-data를 지정하면 실제 DB에 가상 사용자 2명과 의뢰·PDF를 만든다.
이번 실행에서 발급한 사용자만 종료 시 비활성화하며 의뢰·PDF는 삭제하지 않는다.
기존 계정·문서를 수정하지 않고, 비밀번호·쿠키는 출력하거나 파일에 저장하지 않는다.
"""

import argparse
import getpass
import json
import secrets
import string
import sys
import warnings
from datetime import datetime, timezone
from http.client import HTTPException
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4


BASE_URL = "http://127.0.0.1:8000"
MAX_RESPONSE_BYTES = 512 * 1024


class CheckFailed(Exception):
    """인증 정보나 응답 원문 없이 실패를 보고한다."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


def json_body(raw):
    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        raise CheckFailed("JSON 응답이 아닙니다. 서버 로그를 확인하세요.") from None
    require(isinstance(data, dict), "응답 구조가 예상과 다릅니다.")
    return data


def verify(label, response, expected, error_code=None):
    status, _, raw = response
    require(status == expected, f"{label}: 예상 HTTP {expected}, 실제 HTTP {status}")
    data = json_body(raw)
    if error_code:
        error = data.get("error")
        require(isinstance(error, dict) and error.get("code") == error_code,
                f"{label}: 오류 코드가 예상과 다릅니다.")
    print(f"OK: {label} / HTTP {status}")
    return data


def canonical_uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise CheckFailed("응답의 ID가 올바른 UUID가 아닙니다.") from None


class ApiClient:
    def __init__(self):
        self.cookies = CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies), NoRedirect())
        self.logout_needed = False

    def call(self, method, path, data=None, token=None, body=None, content_type=None):
        require(path.startswith("/api/v1/") or path == "/health/", "허용하지 않은 API 경로입니다.")
        require(method in {"GET", "POST"}, "이 확인에서는 GET·POST만 사용합니다.")
        headers = {"Accept": "application/json, application/pdf"}
        if data is not None:
            require(body is None, "JSON과 파일 본문을 동시에 지정할 수 없습니다.")
            body = json.dumps(data).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if token:
            headers["X-CSRFToken"] = token
        request = Request(BASE_URL + path, data=body, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(request, timeout=15)
            except HTTPError as error:
                response = error
            with response:
                status = response.status
                response_headers = response.headers
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (URLError, OSError, HTTPException):
            raise CheckFailed(
                "HTTP 통신에 실패했습니다. POST가 저장되었을 수 있으므로 자동 재전송하지 않습니다."
            ) from None
        require(len(raw) <= MAX_RESPONSE_BYTES, "응답 크기가 확인 한도를 초과했습니다.")
        require(not 300 <= status < 400, f"HTTP {status}: 리다이렉트 설정을 확인하세요.")
        return status, response_headers, raw

    def csrf(self):
        status, _, raw = self.call("GET", "/api/v1/auth/csrf/")
        require(status == 200, f"CSRF 취득 실패: HTTP {status}")
        token = json_body(raw).get("csrf_token")
        require(isinstance(token, str) and bool(token), "CSRF 토큰이 없습니다.")
        return token

    def post(self, path, data):
        return self.call("POST", path, data=data, token=self.csrf())

    def login(self, username, password, role, label):
        token = self.csrf()
        self.logout_needed = True
        data = verify(label, self.call("POST", "/api/v1/auth/login/",
                      data={"username": username, "password": password}, token=token), 200)
        user = data.get("user", {})
        require(isinstance(user, dict) and user.get("username") == username and user.get("role") == role,
                f"{label}: 로그인 계정·역할이 예상과 다릅니다.")

    def logout(self, label):
        if self.logout_needed:
            verify(label, self.post("/api/v1/auth/logout/", {}), 204)
            self.logout_needed = False


def make_pdf():
    """외부 파일 없이 가상 문장 한 줄이 있는 유효한 1쪽 PDF를 만든다."""
    stream = b"BT /F1 12 Tf 20 100 Td (API smoke test. Fictional data only.) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream",
    ]
    content = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(content)
    content.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(content)


def multipart_pdf(key, instruction, filename, pdf):
    boundary = "smoke-" + uuid4().hex
    chunks = []
    for name, value in (("client_request_key", key), ("instruction", instruction)):
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    chunks.append((
        f'--{boundary}\r\nContent-Disposition: form-data; name="pdf"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + pdf + f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def verify_pdf(label, response, expected_pdf):
    status, headers, raw = response
    require(status == 200, f"{label}: 예상 HTTP 200, 실제 HTTP {status}")
    require(headers.get("Content-Type", "").split(";")[0] == "application/pdf", f"{label}: PDF 응답이 아닙니다.")
    require(raw == expected_pdf, f"{label}: 업로드한 PDF와 내려받은 내용이 다릅니다.")
    print(f"OK: {label} / HTTP 200 / PDF 일치")


class Scenario:
    def __init__(self):
        self.tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
        self.admin = ApiClient()
        self.users = []
        self.issued = []
        self.pending_usernames = set()
        self.step = "준비"

    def create_user(self, letter):
        username = "T" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(7))
        password = "Aa1!" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
        full_name = f"API검증_{self.tag}_{letter}"
        print(f"INFO: 이번 실행의 가상 사용자 {letter}: {username}")
        self.pending_usernames.add(username)
        response = self.admin.post("/api/v1/admin/users/", {
            "username": username, "full_name": full_name, "password": password,
        })
        if response[0] == 409:
            # 기존 ID와 충돌한 계정은 후처리 대상으로 넣지 않는다.
            self.pending_usernames.discard(username)
        data = verify(f"일반 사용자 {letter} 발급", response, 201)
        user = data.get("user", {})
        require(isinstance(user, dict) and user.get("username") == username
                and user.get("full_name") == full_name and user.get("role") == "user"
                and user.get("is_active") is True, "발급 응답의 사용자 정보가 예상과 다릅니다.")
        user_id = canonical_uuid(user.get("id"))
        self.issued.append({"id": user_id, "username": username})
        self.pending_usernames.discard(username)
        client = ApiClient()
        self.users.append(client)
        try:
            client.login(username, password, "user", f"일반 사용자 {letter} 로그인")
        finally:
            password = None
        return client

    def intake(self, client, instruction, pdf=None):
        key = str(uuid4())
        print(f"INFO: 접수 확인 키: {key}")
        if pdf is None:
            send = lambda: client.post("/api/v1/requests/", {"client_request_key": key, "instruction": instruction})
        else:
            body, content_type = multipart_pdf(key, instruction, f"api-smoke-{self.tag}.pdf", pdf)
            send = lambda: client.call("POST", "/api/v1/requests/", token=client.csrf(), body=body, content_type=content_type)
        data = verify("PDF 접수" if pdf else "의뢰문 단독 접수", send(), 201)
        record = data.get("request", {})
        require(isinstance(record, dict), "접수 응답 구조가 예상과 다릅니다.")
        request_id = canonical_uuid(record.get("request_id"))
        print(f"INFO: 생성한 의뢰 ID: {request_id}")
        require(record.get("client_request_key") == key and record.get("instruction") == instruction
                and data.get("replayed") is False, "접수 응답의 입력 정보가 예상과 다릅니다.")
        require(record.get("state") == "preparing" and record.get("processing_available") is False,
                "접수 API가 준비 단계와 다르게 동작합니다. AI 연결 여부를 확인하세요.")
        if pdf is None:
            require(record.get("document") is None, "의뢰문 단독 접수에 문서가 반환되었습니다.")
        else:
            document = record.get("document", {})
            require(isinstance(document, dict) and document.get("size_bytes") == len(pdf)
                    and document.get("page_count") == 1, "PDF 메타데이터가 예상과 다릅니다.")
            replay = verify("같은 PDF 접수 재전송", send(), 200)
            require(replay.get("replayed") is True and isinstance(replay.get("request"), dict)
                    and replay["request"].get("request_id") == request_id, "중복 접수가 같은 의뢰를 반환하지 않았습니다.")
        return request_id

    def run(self, username, password):
        self.step = "관리자 로그인"
        self.admin.login(username, password, "admin", self.step)
        password = None
        self.step = "관리자 의뢰 송신 차단"
        denied_key = str(uuid4())
        print(f"INFO: 관리자 차단 확인 키: {denied_key}")
        verify(self.step, self.admin.post("/api/v1/requests/", {
            "client_request_key": denied_key, "instruction": f"API검증 {self.tag}: 관리자 차단 확인",
        }), 403, "FORBIDDEN")
        self.step = "가상 사용자 A 발급·로그인"
        owner = self.create_user("A")
        self.step = "가상 사용자 B 발급·로그인"
        other = self.create_user("B")
        self.step = "의뢰문 접수"
        text_id = self.intake(owner, f"API검증 {self.tag}: 가상 질문입니다.")
        detail = verify("본인 의뢰문 조회", owner.call("GET", f"/api/v1/requests/{text_id}/"), 200)
        require(isinstance(detail.get("request"), dict) and detail["request"].get("request_id") == text_id,
                "본인 의뢰 조회 ID가 일치하지 않습니다.")
        self.step = "PDF 접수·중복 방지"
        pdf = make_pdf()
        request_id = self.intake(owner, f"API검증 {self.tag}: 가상 PDF의 접수 확인입니다.", pdf)
        file_path = f"/api/v1/requests/{request_id}/file/"
        self.step = "본인 PDF 조회"
        verify_pdf(self.step, owner.call("GET", file_path), pdf)
        self.step = "다른 사용자 접근 차단"
        verify("다른 사용자 PDF 조회 차단", other.call("GET", file_path), 404, "DOCUMENT_NOT_FOUND")
        verify("다른 사용자 의뢰 조회 차단", other.call("GET", f"/api/v1/requests/{request_id}/"), 404, "REQUEST_NOT_FOUND")
        self.step = "관리자 PDF 조회"
        verify_pdf(self.step, self.admin.call("GET", file_path), pdf)
        self.step = "관리자 채팅용 조회 차단"
        verify(self.step, self.admin.call("GET", f"/api/v1/requests/{request_id}/"), 403, "FORBIDDEN")

    def cleanup(self):
        """이번 실행에서 201 응답으로 확인한 가상 사용자만 비활성화한다."""
        clean = True
        for index, client in enumerate(self.users, start=1):
            try:
                client.logout(f"가상 사용자 {index} 로그아웃")
                verify(f"가상 사용자 {index} 로그아웃 후 접근 차단",
                       client.call("GET", "/api/v1/auth/me/"), 401, "AUTH_REQUIRED")
            except (CheckFailed, KeyboardInterrupt):
                clean = False
                print(f"WARN: 가상 사용자 {index} 세션 종료 확인 실패")
            finally:
                client.cookies.clear()
        for user in self.issued:
            user_id = user["id"]
            username = user["username"]
            try:
                data = verify("이번 실행의 가상 사용자 비활성화", self.admin.post(
                    f"/api/v1/admin/users/{user_id}/deactivate/", {},
                ), 200)
                result = data.get("user", {})
                require(isinstance(result, dict) and result.get("id") == user["id"]
                        and result.get("is_active") is False, "비활성화 결과 불일치")
            except (CheckFailed, KeyboardInterrupt):
                clean = False
                print(f"WARN: 가상 사용자 비활성화 확인 필요: {username} / {user_id}")
        for username in sorted(self.pending_usernames):
            clean = False
            print(f"WARN: 발급 결과를 확인하지 못한 ID: {username}. 존재 여부와 상태를 별도로 확인하세요.")
        try:
            self.admin.logout("관리자 확인용 세션 로그아웃")
        except (CheckFailed, KeyboardInterrupt):
            clean = False
            print("WARN: 관리자 확인용 세션 종료를 확인하지 못했습니다.")
        finally:
            self.admin.cookies.clear()
        return clean


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-test-data", action="store_true", help="실제 서버에 가상 계정·의뢰·PDF 생성")
    args = parser.parse_args()
    if not args.create_test_data:
        parser.error("실제 데이터가 남습니다. 진행하려면 --create-test-data를 지정하세요.")
    if not sys.stdin.isatty():
        print("FAIL: 대화형 SSH 터미널에서 실행하세요.")
        return 1
    scenario = Scenario()
    print(f"INFO: 실행 식별자: {scenario.tag}")
    print("INFO: 가상 사용자 2명·의뢰 2건·PDF 1개가 저장됩니다. 종료 시 사용자만 비활성화합니다.")
    password = None
    passed = False
    try:
        health = verify("서버·DB 연결", scenario.admin.call("GET", "/health/"), 200)
        require(health.get("django") == "ok" and health.get("database") == "connected", "서버·DB 상태 불일치")
        username = input("관리자 로그인 ID: ").strip()
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("관리자 비밀번호 (화면에 표시되지 않음): ")
        scenario.run(username, password)
        passed = True
    except (CheckFailed, EOFError, KeyboardInterrupt, getpass.GetPassWarning) as error:
        message = str(error) if isinstance(error, CheckFailed) else "입력이 중단되었거나 비밀번호를 숨길 수 없습니다."
        print(f"FAIL: {scenario.step}: {message}")
    finally:
        password = None
        cleaned = scenario.cleanup()
    if passed and cleaned:
        print("PASS: 사용자 발급·의뢰 접수·PDF 권한 확인 완료")
        print("INFO: 가상 사용자들은 비활성화했습니다. 의뢰·PDF는 확인 기록으로 남아 있습니다.")
        return 0
    print("FAIL: 전체 확인을 완료하지 못했습니다. WARN과 실행 식별자를 확인하세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
