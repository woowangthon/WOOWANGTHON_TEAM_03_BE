"""배포된 인증 API를 실제 HTTP로 확인한다.

컨테이너 안에서 실행하며 별도 패키지는 필요하지 않다.
계정·의뢰·PDF를 생성하거나 수정하지 않는다. 로그인으로 실제 DB의
세션과 last_login은 변경될 수 있다. 비밀번호와 쿠키는 파일에 저장하지 않는다.
"""

import argparse
import getpass
import json
import sys
import warnings
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener


BASE_URL = "http://127.0.0.1:8000"
MAX_RESPONSE_BYTES = 512 * 1024


class CheckFailed(Exception):
    """응답 원문이나 인증 정보를 출력하지 않고 확인 실패를 알린다."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # 다른 주소로 인증 정보를 전달하지 않는다.
        return None


class ApiClient:
    def __init__(self):
        self.cookies = CookieJar()
        # 로컬 확인 요청은 환경 변수의 외부 프록시를 거치지 않는다.
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies), NoRedirect())

    def call(self, method, path, data=None, token=None):
        # 확인 스크립트가 업무 데이터를 변경하는 API를 호출하지 않도록 제한한다.
        if method not in {"GET", "POST"} or (
            method == "POST" and path not in {"/api/v1/auth/login/", "/api/v1/auth/logout/"}
        ):
            raise CheckFailed("허용하지 않은 확인 요청입니다.")
        headers = {"Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
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
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (URLError, OSError):
            raise CheckFailed("HTTP 연결에 실패했습니다. backend 실행 상태를 확인하세요.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CheckFailed("응답 크기가 확인 한도를 초과했습니다.")
        if 300 <= status < 400:
            raise CheckFailed(f"HTTP {status}: 리다이렉트 설정을 확인하세요.")
        try:
            result = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            raise CheckFailed(f"HTTP {status}: JSON 응답이 아닙니다. 서버 로그를 확인하세요.") from None
        if not isinstance(result, dict):
            raise CheckFailed(f"HTTP {status}: 응답 구조가 예상과 다릅니다.")
        return status, result

    def csrf(self):
        status, data = self.call("GET", "/api/v1/auth/csrf/")
        token = data.get("csrf_token")
        if status != 200 or not isinstance(token, str) or not token:
            raise CheckFailed(f"CSRF 토큰을 받지 못했습니다. HTTP {status}")
        return token


def verify(label, response, expected, error_code=None):
    status, data = response
    error = data.get("error")
    actual_code = error.get("code") if isinstance(error, dict) else None
    if status != expected or (error_code and actual_code != error_code):
        # 응답 본문·토큰·사용자 정보는 출력하지 않는다.
        raise CheckFailed(f"{label}: 예상 HTTP {expected}, 실제 HTTP {status}")
    print(f"OK: {label} / HTTP {status}")
    return data


def verify_admin(data, username):
    user = data.get("user")
    if not isinstance(user, dict) or user.get("role") != "admin" or user.get("username") != username:
        raise CheckFailed("로그인 사용자 정보가 입력한 관리자와 일치하지 않습니다.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if not sys.stdin.isatty():
        print("FAIL: 대화형 SSH 터미널에서 실행하세요. 비밀번호를 파이프로 전달하지 마세요.")
        return 1

    client = ApiClient()
    cleanup_needed = False
    passed = False
    password = None
    try:
        health = verify("서버·DB 연결", client.call("GET", "/health/"), 200)
        if health.get("django") != "ok" or health.get("database") != "connected":
            raise CheckFailed("서버·DB 상태 응답을 확인하세요.")
        verify("로그인 전 접근 거부", client.call("GET", "/api/v1/auth/me/"), 401, "AUTH_REQUIRED")
        username = input("관리자 로그인 ID: ").strip()
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("관리자 비밀번호 (화면에 표시되지 않음): ")

        token = client.csrf()
        print("OK: CSRF 토큰 취득")
        cleanup_needed = True
        result = client.call("POST", "/api/v1/auth/login/", {"username": username, "password": password}, token)
        password = None
        data = verify("관리자 로그인", result, 200)
        verify_admin(data, username)
        verify_admin(verify("로그인 상태 조회", client.call("GET", "/api/v1/auth/me/"), 200), username)

        data = verify("사용자 목록 조회", client.call("GET", "/api/v1/admin/users/?page_size=1"), 200)
        if not isinstance(data.get("results"), list) or type(data.get("count")) is not int:
            raise CheckFailed("사용자 목록 응답 구조를 확인하세요.")

        # 로그인으로 교체된 CSRF 토큰을 다시 취득한다.
        verify("로그아웃", client.call("POST", "/api/v1/auth/logout/", {}, client.csrf()), 204)
        cleanup_needed = False
        verify("로그아웃 후 접근 거부", client.call("GET", "/api/v1/auth/me/"), 401, "AUTH_REQUIRED")
        passed = True
    except (CheckFailed, EOFError, KeyboardInterrupt, getpass.GetPassWarning) as error:
        message = str(error) if isinstance(error, CheckFailed) else "입력이 중단되었거나 비밀번호를 숨길 수 없습니다."
        print(f"FAIL: {message}")
    finally:
        password = None
        if cleanup_needed:
            try:
                status, _ = client.call("POST", "/api/v1/auth/logout/", {}, client.csrf())
                if status != 204:
                    raise CheckFailed("로그아웃 실패")
                print("INFO: 확인용 세션 로그아웃 완료")
            except (CheckFailed, KeyboardInterrupt):
                passed = False
                print("WARN: 확인용 세션 종료를 확인하지 못했습니다. 서버 상태를 확인하세요.")
        client.cookies.clear()

    if passed:
        print("PASS: 배포된 인증·사용자 목록 API 확인 완료")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
