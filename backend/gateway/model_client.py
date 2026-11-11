"""연구실 PC 모델 서비스(src/server.py, FastAPI 8010)를 Django(백엔드)에서 부르는 클라이언트.

- 표준 라이브러리만 쓴다. 설정은 settings.MODEL_SERVICE_URL / MODEL_API_KEY(X-API-Key 값)이며 processing.model_client() 가 넘긴다.
- 호출 방법·응답 필드는 AI 저장소 "docs/모델 서비스 API v1 (2026-11-03).md" 를 따른다.
- 실패는 예외로 올린다(ModelServiceError 계열). 다만 mode="degraded"(30B 또는 tev1 실패)는 HTTP 200 이라
  예외가 아니라 응답 값으로 드러난다. 저장·화면에서 full 과 구분해야 한다.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Optional

BODY_SEP = "\n\n"  # 서버(src/server.py)의 합치기 규칙과 같아야 한다: 요청문 + 빈 줄 + 본문

# 타임아웃(초). API 문서 1절 권고. urllib 의 timeout 은 소켓 단위라 연결과 각 읽기에 같이 걸린다.
TIMEOUTS = {
    "/health": 10, "/status": 60, "/detect/instant": 10, "/analyze": 180, "/mask": 60, "/restore": 60,
    "/answer_local": 240, "/answer_external": 240, "/embed": 60, "/dict/reload": 60,
}


class ModelServiceError(Exception):
    """모델 서비스 호출 실패의 공통 부모. status 는 HTTP 코드(연결 실패·시간 초과면 None), detail 은 서버가 준 설명."""

    def __init__(self, status: Optional[int], detail: str, path: str, payload: Any = None):
        super().__init__("{} -> {}: {}".format(path, status if status is not None else "연결 실패", detail))
        self.status, self.detail, self.path, self.payload = status, detail, path, payload


class AuthError(ModelServiceError):
    """401 키 없음·틀림"""


class InputError(ModelServiceError):
    """422 빈 글, text·body 동시, 60,000자 초과, 구간 위치가 글 밖"""


class LeakBlocked(ModelServiceError):
    """409 전송 직전 유출 검사에 걸림. 가림 단계의 결함 신호이니 보안팀 로그에 남긴다"""


class RateLimited(ModelServiceError):
    """429 외부 LLM 호출 한도"""


class Unavailable(ModelServiceError):
    """502·503, 연결 실패, 시간 초과. 모델·외부 LLM·서비스 자체가 응답하지 못한 것"""


class ServerError(ModelServiceError):
    """500 그 외"""


def _error_class(status: int):
    return {401: AuthError, 422: InputError, 409: LeakBlocked, 429: RateLimited, 502: Unavailable, 503: Unavailable}.get(status, ServerError)


def _detail(payload: Any, raw: str) -> str:
    """FastAPI 오류 본문에서 사람이 읽을 설명을 뽑는다. 422 는 detail 이 목록이다."""
    if isinstance(payload, dict):
        d = payload.get("detail", payload.get("error"))
        if isinstance(d, str):
            return d
        if isinstance(d, list):
            return "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in d)
        if d is not None:
            return json.dumps(d, ensure_ascii=False)[:300]
    return raw[:300]


def compose(instruction: Optional[str], body: str) -> tuple[str, int]:
    """서버와 같은 규칙으로 요청문과 본문을 합친다. 반환 (합친 글, body_offset).
    구간 위치는 합친 글 기준이므로 본문 하이라이트는 start - body_offset. /mask 에 보내는 text 도 이 합친 글이어야 한다."""
    instr = (instruction or "").strip()
    if instr:
        return instr + BODY_SEP + body, len(instr) + len(BODY_SEP)
    return body, 0


class ModelServiceClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        # .env 에 끝 공백·슬래시가 섞여 들어오는 일이 잦아 앞뒤 공백과 끝 슬래시를 지운다
        base_url = (base_url or os.environ.get("MODEL_SERVICE_URL", "")).strip()
        if not base_url:
            raise ValueError("MODEL_SERVICE_URL 이 비어 있다 (예: http://100.122.114.24:8010)")
        self.base_url = base_url.rstrip("/ ").strip()
        self.api_key = (api_key if api_key is not None else os.environ.get("MODEL_API_KEY", "")).strip()
        # HTTP_PROXY 같은 프록시 환경변수가 있어도 Tailscale 주소는 직접 가야 하므로 프록시를 쓰지 않는다
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, body: Any = None, timeout: Optional[float] = None) -> Any:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key and path != "/health":
            headers["X-API-Key"] = self.api_key
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        t = timeout if timeout is not None else TIMEOUTS.get(path, 60)
        try:
            with self._opener.open(req, timeout=t) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            raise _error_class(e.code)(e.code, _detail(payload, raw), path, payload) from None
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            raise Unavailable(None, "연결 실패 또는 {}초 시간 초과: {}".format(t, e), path) from e
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ServerError(200, "JSON 이 아닌 응답: " + raw[:200], path) from None

    @staticmethod
    def _text_payload(instruction, body, text, request_id, doc_name) -> dict:
        p: dict = {}
        if text is not None:
            p["text"] = text
        else:
            p["instruction"] = instruction
            p["body"] = body
        if request_id is not None:
            p["request_id"] = request_id
        if doc_name is not None:
            p["doc_name"] = doc_name
        return p

    # ---------- 경로별 ----------

    def health(self) -> dict:
        """키 없이. 살아 있는지만."""
        return self._call("GET", "/health")

    def status(self) -> dict:
        """상주 모델, NER 준비, 처리 건수, 버전."""
        return self._call("GET", "/status")

    def detect_instant(self, *, instruction=None, body=None, text=None, request_id=None, doc_name=None) -> dict:
        """즉시 탐지(규칙·사전·NER), 수십 ms. 접수 직후 1차 카드용. 요청문+본문으로 보내면 body_offset 이 돌아온다."""
        return self._call("POST", "/detect/instant", self._text_payload(instruction, body, text, request_id, doc_name))

    def analyze(self, *, instruction=None, body=None, text=None, request_id=None, doc_name=None,
                mask_person: bool = False, mask_company: bool = True, use_llm: bool = True) -> dict:
        """정밀 검사(30B 포함) + 경로 판정 + 결정 + 가림 미리보기. 1~8초, 긴 문서는 2분 넘게 → 배경 작업에서 부른다.
        응답 mode: full / degraded(30B 나 tev1 실패, warnings 에 사유) / instant_only(use_llm=False)."""
        p = self._text_payload(instruction, body, text, request_id, doc_name)
        p["policy"] = {"mask_person": mask_person, "mask_company": mask_company}
        p["use_llm"] = use_llm
        return self._call("POST", "/analyze", p)

    def mask(self, text: str, spans: list, *, mask_person: bool = False, mask_company: bool = True) -> dict:
        """가림 다시 만들기(0ms). text 는 검사 때의 합친 글(compose 결과), spans 는 /analyze 응답의 spans 그대로
        (사람이 해제한 구간은 빼고 보낸다). 승인 카드에서 이름 토글이 바뀌면 이걸로 최신 가림을 만든다."""
        return self._call("POST", "/mask", {"text": text, "spans": spans,
                                            "policy": {"mask_person": mask_person, "mask_company": mask_company}})

    def restore(self, answer: str, mapping: dict) -> dict:
        """외부 답변의 자리표를 원문으로 되돌린다. unresolved 는 대응표에 없어 그대로 둔 자리표(0이어야 정상)."""
        return self._call("POST", "/restore", {"answer": answer, "mapping": mapping})

    def answer_local(self, *, instruction=None, body=None, text=None, request_id=None, max_tokens: int = 1024) -> dict:
        """사내 처리(4.1): 연구실 PC 30B 가 원문으로 답한다. 외부로 나가지 않는다. 수십 초. Ollama 실패는 503."""
        p = self._text_payload(instruction, body, text, request_id, None)
        p["max_tokens"] = max_tokens
        return self._call("POST", "/answer_local", p)

    def answer_external(self, *, masked: str, mapping: dict, request_id=None, model: Optional[str] = None,
                        effort: Optional[str] = None, max_tokens: Optional[int] = None, fallback: bool = True) -> dict:
        """외부 전송(4.2): 승인된 가림 본문(masked)과 대응표(mapping)를 그대로 보내고 답변 복원까지 한 번에 받는다.
        서버가 보내기 직전에 원문 값이 남았는지 검사해 남아 있으면 409(LeakBlocked). 외부 LLM 키가 없으면 503.
        text+spans 로 서버가 가림부터 하는 형식은 일부러 안 쓴다: 승인 카드가 보여준 가림 본문과 달라질 수 있다."""
        p: dict = {"masked": masked, "mapping": mapping, "fallback": fallback}
        if request_id is not None:
            p["request_id"] = request_id
        if model:
            p["model"] = model
        if effort:
            p["effort"] = effort
        if max_tokens:
            p["max_tokens"] = max_tokens
        return self._call("POST", "/answer_external", p)

    def embed(self, texts: list) -> dict:
        """유사 기록 조회용 벡터(1~64건). pgvector vector(1024)."""
        return self._call("POST", "/embed", {"texts": texts})

    def dict_reload(self, companies: Optional[list] = None, projects: Optional[list] = None) -> dict:
        """사전 갱신·재적재. 목록을 준 파일만 통째로 바꾼다. 둘 다 None 이면 파일만 다시 읽는다."""
        p: dict = {}
        if companies is not None:
            p["companies"] = companies
        if projects is not None:
            p["projects"] = projects
        return self._call("POST", "/dict/reload", p)

