"""실행 중인 서버에서 접수 → 처리 시작 → 검사 → (승인 대기 → 승인 → 외부 전송) → 답변 조회의 전체 흐름을 확인한다.

가상 문서로만 쓴다. 비밀번호·쿠키·검사 원문·답변 전문은 출력하지 않는다(--show-answer N 으로 앞 N자만).
--approve 는 실제 외부 LLM 호출(비용 발생)이 일어나므로 의도한 경우에만 지정한다. 지정하지 않으면 승인 대기에서 멈추고
의뢰는 30분 뒤 만료된다(--reject 로 바로 거절할 수 있다).
비밀번호는 환경변수(--password-env, 기본 CHECK_PASSWORD)에서 읽고 없으면 화면 입력을 받는다.

예: CHECK_PASSWORD=... python scripts/check_processing_flow.py --username USR90001 --pdf fictional.pdf --approve
"""

import argparse
import getpass
import json
import os
import sys
import time
from collections import Counter
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

RUNNING = {"queued", "analyzing", "processing"}


class CheckFailed(Exception):
    """인증 정보나 응답 원문 없이 실패를 보고한다."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def multipart(fields, file_field, filename, content):
    boundary = "----check" + uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode("utf-8") + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Session:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.cookies = CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies), NoRedirect())
        self.logged_in = False

    def call(self, method, path, data=None, body=None, content_type=None, token=None, timeout=30):
        headers = {"Accept": "application/json"}
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if token:
            headers["X-CSRFToken"] = token
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(request, timeout=timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.status
                raw = response.read(1024 * 1024)
        except (URLError, OSError) as error:
            raise CheckFailed(f"HTTP 통신 실패: {type(error).__name__}") from None
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"raw": raw[:200].decode("utf-8", "replace")}
        return status, payload

    def csrf(self):
        status, payload = self.call("GET", "/api/v1/auth/csrf/")
        if status != 200 or not payload.get("csrf_token"):
            raise CheckFailed(f"CSRF 토큰 취득 실패: HTTP {status}")
        return payload["csrf_token"]

    def post(self, path, data=None, body=None, content_type=None):
        return self.call("POST", path, data=data, body=body, content_type=content_type, token=self.csrf())

    def login(self, username, password):
        status, payload = self.post("/api/v1/auth/login/", {"username": username, "password": password})
        if status != 200:
            raise CheckFailed(f"로그인 실패: HTTP {status} {payload.get('error', {}).get('code', '')}")
        self.logged_in = True
        print(f"OK: 로그인 {username} ({payload.get('user', {}).get('role')})")

    def logout(self):
        if self.logged_in:
            status, _ = self.post("/api/v1/auth/logout/", {})
            self.logged_in = False
            print(f"OK: 로그아웃 / HTTP {status}")


def expect(label, status, payload, expected):
    if status != expected:
        error = payload.get("error", {})
        raise CheckFailed(f"{label}: 예상 HTTP {expected}, 실제 {status} {error.get('code', '')} {error.get('message', '')}")
    print(f"OK: {label} / HTTP {status}")
    return payload


def wait_for(session, request_id, running, limit):
    started = time.monotonic()
    last = None
    while True:
        status, payload = session.call("GET", f"/api/v1/requests/{request_id}/")
        if status != 200:
            raise CheckFailed(f"상세 조회 실패: HTTP {status} {payload.get('error', {}).get('code', '')}")
        record = payload["request"]
        if record["state"] != last:
            print(f"  상태 {record['state']} ({time.monotonic() - started:.1f}초)")
            last = record["state"]
        if record["state"] not in running:
            return record, time.monotonic() - started
        if time.monotonic() - started > limit:
            raise CheckFailed(f"{limit}초 안에 처리가 끝나지 않았습니다(현재 {record['state']}).")
        time.sleep(2)


def masked_rendering(preview):
    text = preview["input_text"]
    for span in sorted(preview["detection_spans"], key=lambda s: -s["start_offset"]):
        text = text[:span["start_offset"]] + f"[{span['label'].upper()}]" + text[span["end_offset"]:]
    return text


def run(args, password):
    user = Session(args.base_url)
    user.login(args.username, password)
    key = str(uuid4())
    if args.pdf:
        with open(args.pdf, "rb") as handle:
            content = handle.read()
        body, content_type = multipart(
            {"client_request_key": key, "instruction": args.instruction}, "pdf", os.path.basename(args.pdf), content,
        )
        status, payload = user.post("/api/v1/requests/", body=body, content_type=content_type)
    else:
        status, payload = user.post("/api/v1/requests/", {"client_request_key": key, "instruction": args.instruction})
    record = expect("접수", status, payload, 201)["request"]
    request_id = record["request_id"]
    print(f"INFO: 의뢰 ID {request_id} / 상태 {record['state']} / 처리 시작 가능 {record['processing_available']}"
          + (f" / 문서 {record['document']['extraction_status']} {record['document']['page_count']}쪽" if record["document"] else ""))
    if record["instruction"] != args.instruction:
        raise CheckFailed("접수된 의뢰문이 보낸 값과 다릅니다. 클라이언트 인코딩(UTF-8)을 확인하세요.")
    if not record["processing_available"]:
        raise CheckFailed("접수 직후 처리 시작이 불가로 표시됩니다. 의뢰문과 문서 상태를 확인하세요.")

    status, payload = user.post(f"/api/v1/requests/{request_id}/start/", {})
    record = expect("처리 시작", status, payload, 202)["request"]
    if record["document"]:
        print(f"INFO: 문서 추출 상태 {record['document']['extraction_status']}")
    # 사내 경로는 analyzing 뒤 processing(사내 답변 생성)까지 기다린다. 외부 경로는 awaiting_approval 에서 멈춘다.
    record, elapsed = wait_for(user, request_id, RUNNING, args.wait)
    print(f"INFO: 검사 결과 상태 {record['state']} ({elapsed:.1f}초)")

    if record["state"] == "awaiting_approval":
        status, payload = user.call("GET", f"/api/v1/requests/{request_id}/approval-preview/")
        preview = expect("승인 전 사전 확인 조회", status, payload, 200)["preview"]
        labels = Counter(span["label"] for span in preview["detection_spans"])
        sources = Counter(span["input_source"] for span in preview["detection_spans"])
        print(f"INFO: 검출 {preview['detection_count']}건 라벨 {dict(labels)} 출처 {dict(sources)} / 기한 {preview['approval']['expires_at']}")
        if args.show_preview:
            print("---- 가림 미리보기(앞 600자) ----")
            print(masked_rendering(preview)[:600])
            print("----")
        if args.approve or args.reject:
            decision = "approved" if args.approve else "rejected"
            status, payload = user.post(f"/api/v1/requests/{request_id}/approval/", {"decision": decision})
            result = expect(f"외부 전송 {decision}", status, payload, 200)["request"]
            print(f"INFO: 결정 뒤 상태 {result['state']} / 승인 {result['approval']['status']}")
            if args.approve:
                record, elapsed = wait_for(user, request_id, RUNNING, args.wait)
                print(f"INFO: 외부 전송 뒤 상태 {record['state']} ({elapsed:.1f}초)")
        else:
            print("INFO: --approve/--reject 를 지정하지 않아 승인 대기 상태로 둡니다(30분 뒤 만료).")

    if record["state"] == "completed":
        answer = record.get("answer_text") or ""
        print(f"OK: 완료. 답변 {len(answer)}자")
        if args.show_answer:
            print("---- 답변 앞부분 ----")
            print(answer[:args.show_answer])
            print("----")
    elif record["state"] not in ("awaiting_approval", "rejected"):
        print(f"WARN: 의뢰가 {record['state']} 로 끝났습니다. 관리자 상세 API 에서 failure 코드를 확인하세요.")
    user.logout()

    if args.admin_username:
        admin_password = os.environ.get(args.admin_password_env) or getpass.getpass("관리자 비밀번호 (화면에 표시되지 않음): ")
        admin = Session(args.base_url)
        admin.login(args.admin_username, admin_password)
        status, payload = admin.call("GET", f"/api/v1/admin/requests/{request_id}/")
        detail = expect("관리자 감사 상세 조회", status, payload, 200)["request"]
        print(json.dumps({
            "state": detail["state"], "inspection_status": detail["inspection_status"],
            "processing_route": detail["processing_route"], "detection_count": detail["detection_count"],
            "detection_labels": detail["detection_labels"], "masking_mappings": len(detail["masking_mappings"]),
            "warning_codes": detail["warning_codes"], "approval": detail["approval"], "delivery": detail["delivery"],
            "failure": detail["failure"], "inspection_versions": detail["inspection_versions"],
            "answer_in_admin_view": "answer_text" in detail,
        }, ensure_ascii=False, indent=1))
        admin.logout()
    return request_id


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", required=True, help="일반 이용자 계정")
    parser.add_argument("--password-env", default="CHECK_PASSWORD", help="비밀번호를 담은 환경변수 이름")
    parser.add_argument("--pdf", help="접수할 가상 PDF 경로(없으면 의뢰문만)")
    parser.add_argument("--instruction", default="아래 문서를 요약해 주세요.")
    parser.add_argument("--approve", action="store_true", help="승인 대기면 승인해 실제 외부 전송까지 확인(비용 발생)")
    parser.add_argument("--reject", action="store_true", help="승인 대기면 거절")
    parser.add_argument("--wait", type=int, default=300, help="각 단계 최대 대기 초, 기본 300")
    parser.add_argument("--show-preview", action="store_true", help="가림 미리보기 앞 600자 출력")
    parser.add_argument("--show-answer", type=int, default=0, metavar="N", help="답변 앞 N자 출력")
    parser.add_argument("--admin-username", help="지정하면 관리자 감사 상세도 조회")
    parser.add_argument("--admin-password-env", default="CHECK_ADMIN_PASSWORD")
    args = parser.parse_args()
    if args.approve and args.reject:
        parser.error("--approve 와 --reject 는 같이 쓸 수 없습니다.")
    try:
        password = os.environ.get(args.password_env) or getpass.getpass("이용자 비밀번호 (화면에 표시되지 않음): ")
        request_id = run(args, password)
    except (CheckFailed, EOFError, KeyboardInterrupt) as error:
        print(f"FAIL: {error}")
        return 1
    print(f"DONE: 의뢰 {request_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
