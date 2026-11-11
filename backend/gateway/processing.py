"""AI 저장자: 접수된 의뢰를 모델 서비스(AI 저장소 FastAPI)로 검사·답변하고 결과를 gateway 테이블에 저장한다.

흐름(Request.State)
  preparing ─start_processing→ queued ─run_analysis→ analyzing ─┬─ external → prepare_approval(TK-63) → awaiting_approval
                                                                │       승인 API(queued/approved) ─run_external→ processing → completed
                                                                ├─ local → processing → /answer_local → completed
                                                                ├─ block → blocked
                                                                └─ 불완전 검사·호출 실패 → failed
규칙
- input_text = 의뢰문 + 빈 줄(LF 두 개) + PDF 추출 본문. PDF 가 없으면 의뢰문 그대로. 정규화하지 않는다(intake_service 와 같은 규칙).
- 구간 위치는 input_text 의 코드포인트 기준, 끝 미포함. 본문 시작 위치 앞이면 instruction, 뒤면 pdf 구간이다.
- PDF 본문은 비공개 저장소(storage_object_key)에서 읽어 pymupdf 로 뽑는다(pdf.py, AI 저장소 src/extract.py 와 같은 규칙).
- 구간 하나 = SensitiveTerm(value, label) + DetectionSpan. 가려진 구간은 MaskingMapping(placeholder) 을 연결한다.
  겹침 등 제약 위반으로 저장하지 못한 구간이 있으면 경고 코드(SPAN_SAVE_SKIPPED)를 남기고 실패로 끝낸다.
- 외부 전송 승인 기록은 approval_decision.decide_approval(queued/approved) 이 하고, 실제 전송은 run_external 이 한다.
  전송문은 저장하지 않고 승인 뒤 DB 의 구간·대응표로 다시 만든다(build_masked_text). 그래서 검사 직후, 다시 만든 전송문과
  대응표가 모델 서비스가 가린 결과(/analyze 의 mask)와 같은지 본다(mask_matches). 다르면 저장되지 않은 구간의 원문이
  전송문에 남으므로 승인 대기로 보내지 않고 실패로 끝낸다(MASK_MISMATCH).
- 정책(BE 요구사항 v1.0): 인명 항상 가림, 구간 0건도 외부 전송 전 승인, 불완전 검사(degraded)는 실패·전송 금지,
  답변 절단·복원 불완전은 실패. 실패는 failed + error_stage/error_code 로 남기고 재시도는 새 의뢰다.
- 배경 실행: GATEWAY_WORKER=thread 면 커밋 뒤 배경 스레드, 아니면 `manage.py process_requests` 가 queued 를 가져간다.
- 결과 저장: 작업자는 모델 호출을 마친 뒤 행을 다시 잠가, 아직 자기가 가져간 상태일 때만 결과를 쓴다(apply_result).
  그 사이 끝난 의뢰(장시간 진행 정리 등)의 늦은 결과는 버린다. 작업자의 열(RESULT_FIELDS)만 갱신해 숨김 같은 다른 쪽의
  변경을 덮어쓰지 않는다. 외부 전송 여부만은 의뢰 상태와 무관한 사실이라 늦게라도 남긴다(record_delivery).
"""
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from .approval_lifecycle import prepare_approval
from .intake_service import locked_request_user
from .intake_validation import IntakeError
from .model_client import (
    AuthError, InputError, LeakBlocked, ModelServiceClient, ModelServiceError, RateLimited, ServerError, Unavailable,
)
from .models import DetectionSpan, Document, MaskingMapping, Request, SensitiveTerm
from .pdf import PdfError, extract_pdf
from .storage import document_storage

log = logging.getLogger("gateway.processing")
S = Request.State
BODY_SEP = "\n\n"
MODEL_LOCK = threading.Lock()  # 30B 를 쓰는 호출은 한 프로세스에서 한 번에 하나(모델 서비스가 직렬 처리)
SUPPORTED_LABELS = set(SensitiveTerm.Label.values)

# 모델 서비스의 한국어 경고 문장 → 저장용 코드. 문장 일부로 판별한다.
WARNING_CODES = (
    ("지시 삽입 감지", "INJECTION_DETECTED"),
    ("정밀 검사(30B) 호출 실패", "LLM_FAILED"),
    ("생성 상한에서 끊김", "LLM_OUTPUT_TRUNCATED"),
    ("라우팅(tev1) 호출 실패", "ROUTE_FAILED"),
)
CODE_ROUTE_INPUT_TRUNCATED = "ROUTE_INPUT_TRUNCATED"   # 경로 판정 모델이 앞부분만 봄
CODE_LLM_SPANS_DROPPED = "LLM_SPANS_DROPPED"           # 30B 가 원문에 없는 값을 돌려줘 버림
CODE_LABEL_UNSUPPORTED = "SPAN_LABEL_UNSUPPORTED"      # DB 라벨 7종에 없는 라벨의 구간을 버림
CODE_SPAN_SKIPPED = "SPAN_SAVE_SKIPPED"                # 제약 위반(겹침 등)으로 구간을 저장하지 못함. 경고이자 실패 사유(error_code)
CODE_MASK_MISMATCH = "MASK_MISMATCH"                   # 다시 만든 전송문·대응표가 모델 서비스의 가림 결과와 다름(error_code)
ERROR_CODES = (
    (AuthError, "MODEL_AUTH"), (InputError, "MODEL_INPUT"), (LeakBlocked, "LEAK_BLOCKED"),
    (RateLimited, "EXTERNAL_RATE_LIMITED"), (Unavailable, "MODEL_UNAVAILABLE"), (ServerError, "MODEL_SERVER_ERROR"),
)
# 외부 전송 요청이 모델 서비스 안에서 거부되어 외부로 나가지 않은 경우(delivery_status=not_sent 로 확정)
NOT_SENT_ERRORS = (AuthError, InputError, LeakBlocked)
# 작업자가 쓰는 열. 결과는 이 열만 갱신해, 처리 중에 다른 쪽이 바꾼 열(숨김·승인 등)을 건드리지 않는다.
RESULT_FIELDS = (
    "state", "inspection_status", "inspection_versions", "warning_codes", "processing_route", "error_stage", "error_code",
    "delivery_status", "answer_text", "finished_at", "updated_at",
)


def compose_input(instruction: str, body: str) -> str:
    """검사 입력. intake_service.update_instruction 의 합계 검사와 같은 규칙이어야 한다."""
    return instruction + BODY_SEP + body if body else instruction


def processing_available(record, document) -> bool:
    """처리 시작 API 를 부를 수 있는 상태인지(화면의 시작 버튼 근거)."""
    waiting = (Document.ExtractionStatus.PENDING, Document.ExtractionStatus.EXTRACTING, Document.ExtractionStatus.READY)
    return (
        record.state == S.PREPARING and bool(record.instruction.strip())
        and (document is None or document.extraction_status in waiting)
    )


def model_client() -> ModelServiceClient:
    """테스트에서 가짜 클라이언트로 바꿔 끼우는 지점."""
    url = (getattr(settings, "MODEL_SERVICE_URL", "") or "").strip()
    if not url:
        raise Unavailable(None, "MODEL_SERVICE_URL 설정이 비어 있습니다.", "/")
    return ModelServiceClient(base_url=url, api_key=getattr(settings, "MODEL_API_KEY", ""))


def warning_code(text) -> str:
    for needle, code in WARNING_CODES:
        if needle in str(text):
            return code
    return "WARNING_OTHER"


def error_code_for(exc: ModelServiceError) -> str:
    for cls, code in ERROR_CODES:
        if isinstance(exc, cls):
            return code
    return "MODEL_ERROR"


def add_codes(record: Request, codes) -> None:
    current = list(record.warning_codes or [])
    for code in codes:
        if code and code not in current:
            current.append(code)
    record.warning_codes = current


def fail(record: Request, stage: str, code: str, *, inspection_failed: bool = False) -> Request:
    """실패를 failed + error_stage/error_code 로 남긴다. 원문·답변·구간은 지우지 않는다.
    record 는 잠금 아래 방금 읽은 행이어야 한다(locked·apply_result). 오래 들고 있던 사본에는 쓰지 않는다."""
    record.state = S.FAILED
    record.error_stage = stage[:64]
    record.error_code = code[:100]
    if inspection_failed:
        record.inspection_status = Request.InspectionStatus.FAILED
    record.finished_at = timezone.now()
    record.save(update_fields=RESULT_FIELDS)
    log.warning("request %s failed at %s: %s", record.pk, stage, code)
    return record


# ---------- 배경 실행 ----------

def launch(worker, request_id) -> None:
    """현재 트랜잭션이 커밋된 뒤 배경 스레드에서 worker(request_id) 를 돌린다(자동 커밋이면 즉시).
    GATEWAY_WORKER 가 thread 가 아니면 아무것도 하지 않고 process_requests 명령이 queued 를 가져간다."""
    if getattr(settings, "GATEWAY_WORKER", "thread") != "thread":
        return

    def start():
        threading.Thread(
            target=run_guarded, args=(worker, request_id), name=f"{worker.__name__}-{request_id}", daemon=True,
        ).start()

    transaction.on_commit(start)


def run_safely(worker, request_id) -> bool:
    """worker(request_id) 를 돌린다. 예상 밖 예외는 failed(INTERNAL_ERROR) 로 남기고 삼킨다. 예외 없이 끝났으면 True.
    의뢰 하나의 오류가 배경 스레드나 정기 실행(dispatch_pending)을 멈추지 않게 한다."""
    try:
        worker(request_id)
        return True
    except Exception:  # noqa: BLE001 - 배경 작업은 예외를 올릴 곳이 없다
        log.exception("request %s: worker %s crashed", request_id, worker.__name__)
    try:
        with transaction.atomic():
            record = locked(request_id, state__in=(S.QUEUED, S.ANALYZING, S.PROCESSING))
            if record is not None:
                fail(record, "system", "INTERNAL_ERROR")
    except Exception:  # noqa: BLE001
        log.exception("request %s: could not record the failure", request_id)
    return False


def run_guarded(worker, request_id) -> None:
    """스레드 본체. 끝나면 스레드의 DB 연결을 닫는다."""
    try:
        run_safely(worker, request_id)
    finally:
        connections.close_all()


def locked(request_id, **filters):
    """잠금 아래 최신 행을 읽는다. 조건에 안 맞으면 None. transaction.atomic() 안에서 부른다."""
    return Request.objects.select_for_update().filter(pk=request_id, **filters).first()


def claim(request_id, *, filters: dict, **changes):
    """잠금 아래 조건을 확인하고 상태를 바꾼다. 조건에 안 맞으면(다른 작업자가 먼저 가져갔거나 상태가 바뀜) None."""
    with transaction.atomic():
        record = locked(request_id, **filters)
        if record is None:
            return None
        for name, value in changes.items():
            setattr(record, name, value)
        record.save(update_fields=[*changes, "updated_at"])
    return record


def apply_result(request_id, state, change):
    """작업자가 모델 호출을 마치고 결과를 쓸 때 쓴다. 행을 다시 잠가 최신 값을 읽고, 아직 작업자가 가져간 상태(state)일 때만
    change(record) 로 결과를 적용한다(change 는 fail·complete 처럼 저장까지 한다).
    그 사이 다른 쪽이 의뢰를 끝냈으면(장시간 진행 정리 등) 늦은 결과를 버리고 None 을 돌려준다."""
    with transaction.atomic():
        record = locked(request_id, state=state)
        if record is None:
            log.warning("request %s: late result discarded (no longer %s)", request_id, state)
            return None
        change(record)
    return record


def record_delivery(request_id, status) -> None:
    """외부 전송 요청이 밖으로 나갔는지를 남긴다(전송 시작 때 적어 둔 unknown 을 status 로).
    의뢰 상태와 무관한 사실이라, 응답을 기다리는 사이 실패로 정리된 의뢰에도 기록한다."""
    with transaction.atomic():
        record = locked(request_id, delivery_status=Request.DeliveryStatus.UNKNOWN)
        if record is not None:
            record.delivery_status = status
            record.save(update_fields=["delivery_status", "updated_at"])


# ---------- 처리 시작 ----------

def start_processing(authenticated_user, request_id):
    """본인의 준비 중 의뢰를 검사 대기(queued)로 바꾼다. PDF 본문을 뽑아 input_text 를 고정하고 커밋 뒤 검사를 예약한다.
    반환 (요청, 문서). 입력·상태 문제는 IntakeError 로 올린다(처리 시작 API 가 그대로 응답한다)."""
    extraction_error = None
    try:
        with transaction.atomic():
            user = locked_request_user(authenticated_user)
            record = Request.objects.select_for_update().filter(
                pk=request_id, user=user, hidden_at__isnull=True,
            ).first()
            if record is None:
                raise IntakeError("REQUEST_NOT_FOUND", "의뢰를 찾을 수 없습니다.", 404)
            if record.state != S.PREPARING:
                raise IntakeError("PROCESSING_ALREADY_STARTED", "이미 처리를 시작한 의뢰입니다.", 409)
            if not record.instruction.strip():
                raise IntakeError("INSTRUCTION_REQUIRED", "의뢰문을 입력한 뒤 처리를 시작할 수 있습니다.", 400)
            document = Document.objects.select_for_update().filter(request=record).first()
            try:
                body = document_text(document) if document is not None else ""
            except PdfError as exc:
                extraction_error = exc  # 실패 기록은 이 트랜잭션 밖에서 저장한다(예외로 되돌리지 않기 위해)
            else:
                text = compose_input(record.instruction, body)
                if len(text) > settings.INPUT_MAX_CHARS:
                    raise IntakeError("INPUT_TOO_LONG", "의뢰문과 PDF 본문은 합계 60,000문자 이내여야 합니다.", 400)
                record.input_text = text
                record.state = S.QUEUED
                record.save(update_fields=["input_text", "state", "updated_at"])
                launch(run_analysis, record.pk)
    except ValidationError:
        raise IntakeError("REQUEST_UPDATE_FAILED", "의뢰를 저장하지 못했습니다.", 503) from None
    if extraction_error is not None:
        record_extraction_failure(record.pk, extraction_error)
        raise IntakeError(extraction_error.code, extraction_error.message, 400)
    return record, document


def document_text(document: Document) -> str:
    """추출 완료 본문을 돌려준다. 추출 대기면 비공개 저장소의 원본에서 지금 뽑아 ready 로 저장한다."""
    status = Document.ExtractionStatus
    if document.extraction_status == status.READY:
        if document.extracted_text is None:
            raise IntakeError("DOCUMENT_NOT_READY", "문서 처리 상태를 확인할 수 없습니다.", 409)
        return document.extracted_text
    if document.extraction_status == status.FAILED:
        raise IntakeError("REQUEST_NOT_EDITABLE", "실패한 파일은 새 의뢰로 제출해 주세요.", 409)
    if document.extraction_status not in (status.PENDING, status.EXTRACTING):
        raise IntakeError("DOCUMENT_NOT_READY", "PDF 업로드가 끝나지 않았습니다.", 409)
    if not document.storage_object_key:
        raise IntakeError("DOCUMENT_NOT_AVAILABLE", "원본 파일을 열 수 없습니다.", 409)
    try:
        with document_storage().open(document.storage_object_key, "rb") as source:
            data = source.read(settings.PDF_MAX_BYTES + 1)
    except (OSError, SuspiciousFileOperation):
        raise IntakeError("DOCUMENT_UNAVAILABLE", "원본 파일을 불러오지 못했습니다.", 503) from None
    extracted = extract_pdf(data)
    document.extracted_text = extracted.text
    if document.page_count is None:
        document.page_count = extracted.page_count
    document.extraction_status = status.READY
    document.save(update_fields=["extracted_text", "page_count", "extraction_status"])
    return extracted.text


def record_extraction_failure(request_id, exc: PdfError) -> None:
    """추출 실패는 문서·의뢰 모두 failed 로 남긴다. 같은 의뢰로 다시 시작할 수 없다(새 의뢰로 접수)."""
    with transaction.atomic():
        record = Request.objects.select_for_update().get(pk=request_id)
        Document.objects.filter(request=record).update(extraction_status=Document.ExtractionStatus.FAILED)
        fail(record, "extraction", exc.code)


# ---------- 검사 ----------

def run_analysis(request_id):
    """queued → analyzing. 모델 서비스 /analyze 결과를 저장하고 결정(block/local/external)에 따라 상태를 정한다."""
    record = claim(request_id, filters={"state": S.QUEUED, "approval_status__isnull": True}, state=S.ANALYZING)
    if record is None:
        return None
    document = Document.objects.filter(request=record).first()
    try:
        with MODEL_LOCK:
            response = model_client().analyze(
                text=record.input_text, request_id=str(record.pk),
                doc_name=document.original_filename if document is not None else None,
                mask_person=True, mask_company=True,  # 인명 항상 가림(토글 없음)
            )
    except ModelServiceError as exc:
        log.warning("request %s: /analyze failed: %s", record.pk, exc)
        code = error_code_for(exc)
        return apply_result(record.pk, S.ANALYZING, lambda fresh: fail(fresh, "inspection", code, inspection_failed=True))
    body_start = len(record.instruction) + len(BODY_SEP) if document is not None else None
    return apply_analysis(record.pk, response, body_start)


def apply_analysis(request_id, response: dict, body_start):
    """/analyze 응답을 저장하고 다음 단계로 넘긴다. body_start 는 input_text 에서 PDF 본문이 시작하는 위치(PDF 없으면 None).
    행을 잠근 트랜잭션에서 구간·검사 결과를 저장하고 승인 대기 전환(prepare_approval)까지 한다.
    검사 중(analyzing)이 아니면 그 사이 끝난 의뢰이므로 늦은 결과를 버리고 None 을 돌려준다."""
    mode = response.get("mode") or ""
    decision = response.get("decision") or {}
    route = response.get("route") or {}
    Route = Request.ProcessingRoute
    with transaction.atomic():
        record = locked(request_id, state=S.ANALYZING)
        if record is None:
            log.warning("request %s: late /analyze result discarded (no longer %s)", request_id, S.ANALYZING)
            return None
        complete = mode == "full"
        record.inspection_status = Request.InspectionStatus.COMPLETE if complete else Request.InspectionStatus.DEGRADED
        versions = dict(response.get("versions") or {})
        versions["mode"] = mode
        record.inspection_versions = versions
        warnings = list(response.get("warnings") or []) + list(decision.get("warnings") or [])
        codes = [warning_code(text) for text in warnings]
        if route.get("truncated"):
            codes.append(CODE_ROUTE_INPUT_TRUNCATED)
        if response.get("dropped"):
            codes.append(CODE_LLM_SPANS_DROPPED)
        saved, skipped, unsupported = save_spans(record, response.get("spans") or [], body_start)
        if unsupported:
            codes.append(CODE_LABEL_UNSUPPORTED)
        if skipped:
            codes.append(CODE_SPAN_SKIPPED)
        add_codes(record, codes)
        action = decision.get("action")
        # 검사 결과를 그대로 쓸 수 없는 사유(error_code). 저장하지 못한 구간이나 전송문 불일치는 가려지지 않은 원문이 남는다는 뜻이다.
        problem = None
        if not complete:
            problem = "INSPECTION_DEGRADED"
        elif skipped:
            problem = CODE_SPAN_SKIPPED
        elif action == "external" and not mask_matches(record, response.get("mask")):
            problem = CODE_MASK_MISMATCH
        now = timezone.now()
        if action == "block":
            record.processing_route = Route.BLOCKED
            record.state, record.finished_at = S.BLOCKED, now
        elif problem:
            # 결과를 남기되 실패로 끝낸다. 외부 전송은 물론 사내 답변도 만들지 않는다.
            record.processing_route = {"local": Route.LOCAL, "external": Route.EXTERNAL}.get(action)
            record.state, record.finished_at = S.FAILED, now
            record.error_stage, record.error_code = "inspection", problem
        elif action == "local":
            record.processing_route = Route.LOCAL
            record.state = S.PROCESSING
        elif action == "external":
            # 구간 0건이어도 승인 필수. 상태 전환과 30분 기한은 prepare_approval(TK-63)이 한다.
            record.processing_route = Route.EXTERNAL
            record.delivery_status = Request.DeliveryStatus.NOT_SENT
        else:
            record.state, record.finished_at = S.FAILED, now
            record.error_stage, record.error_code = "decision", "DECISION_UNKNOWN"
        record.save(update_fields=RESULT_FIELDS)
        if record.state == S.ANALYZING and record.processing_route == Route.EXTERNAL:
            try:
                record, _ = prepare_approval(record.pk)
            except IntakeError as exc:
                log.warning("request %s: prepare_approval refused: %s", record.pk, exc.code)
                fail(record, "approval", exc.code)
    log.info("request %s analyzed: mode=%s action=%s spans=%d skipped=%d unsupported=%d codes=%s",
             record.pk, mode, action, saved, skipped, unsupported, record.warning_codes)
    if record.state == S.PROCESSING:
        return run_local_answer(record)
    return record


def save_spans(record: Request, spans: list, body_start) -> tuple:
    """구간 → SensitiveTerm + DetectionSpan(+MaskingMapping). 반환 (저장, 제약으로 건너뜀, 라벨 미지원).
    값(원문)은 로그에 남기지 않는다."""
    saved = skipped = unsupported = 0
    mappings: dict = {}
    for span in sorted(spans, key=lambda s: (int(s.get("start", 0)), int(s.get("end", 0)))):
        label = span.get("label")
        value = span.get("text") or ""
        if label not in SUPPORTED_LABELS or not value.strip():
            unsupported += 1
            log.info("request %s: span label %r is not a DB label, dropped", record.pk, label)
            continue
        start, end = int(span["start"]), int(span["end"])
        if body_start is not None and start >= body_start:
            source = DetectionSpan.InputSource.PDF
        else:
            source = DetectionSpan.InputSource.INSTRUCTION
        try:
            with transaction.atomic():
                term, _ = SensitiveTerm.objects.get_or_create(value=value, label=label)
                mapping = None
                placeholder = span.get("placeholder")
                if span.get("masked") and placeholder:
                    mapping = mappings.get(term.pk) or MaskingMapping.objects.filter(
                        request=record, sensitive_term=term,
                    ).first()
                    if mapping is None:
                        mapping = MaskingMapping.objects.create(request=record, sensitive_term=term, placeholder=placeholder)
                    mappings[term.pk] = mapping
                DetectionSpan.objects.create(
                    request=record, sensitive_term=term, input_source=source,
                    start_offset=start, end_offset=end, mapping=mapping,
                )
            saved += 1
        except (ValidationError, IntegrityError, ValueError) as exc:
            skipped += 1
            log.warning("request %s: span %d-%d (%s) skipped: %s", record.pk, start, end, label, type(exc).__name__)
    return saved, skipped, unsupported


# ---------- 답변 ----------

def complete(record: Request, answer, truncated, stage: str) -> Request:
    """최종 답변 저장. 잘렸거나 비어 있으면 실패로 남긴다(상세 조회는 completed 에서만 답변을 낸다).
    record 는 fail 과 같이 잠금 아래 방금 읽은 행이어야 한다."""
    if truncated:
        return fail(record, stage, "ANSWER_TRUNCATED")
    if not isinstance(answer, str) or not answer.strip():
        return fail(record, stage, "ANSWER_EMPTY")
    record.answer_text = answer
    record.state, record.finished_at = S.COMPLETED, timezone.now()
    record.save(update_fields=RESULT_FIELDS)
    log.info("request %s completed via %s", record.pk, stage)
    return record


def run_local_answer(record: Request):
    """사내 처리: 연구실 PC 30B 가 원문으로 답한다. 외부로 나가지 않는다."""
    try:
        with MODEL_LOCK:
            response = model_client().answer_local(
                text=record.input_text, request_id=str(record.pk),
                max_tokens=getattr(settings, "GATEWAY_LOCAL_MAX_TOKENS", 1024),
            )
    except ModelServiceError as exc:
        log.warning("request %s: /answer_local failed: %s", record.pk, exc)
        code = error_code_for(exc)
        return apply_result(record.pk, S.PROCESSING, lambda fresh: fail(fresh, "local_answer", code))

    def save_answer(fresh):
        add_codes(fresh, [warning_code(text) for text in response.get("warnings") or []])
        complete(fresh, response.get("answer"), response.get("truncated"), "local_answer")

    return apply_result(record.pk, S.PROCESSING, save_answer)


def mapping_dict(record: Request) -> dict:
    """{자리표: 원문값}. /answer_external 의 mapping 과 복원에 쓴다."""
    rows = record.masking_mappings.select_related("sensitive_term")
    return {mapping.placeholder: mapping.sensitive_term.value for mapping in rows}


def build_masked_text(record: Request) -> str:
    """input_text 에서 대응표가 있는 구간을 자리표로 바꾼 전송문. 뒤에서부터 치환해 위치가 밀리지 않게 한다."""
    text = record.input_text
    spans = record.detection_spans.filter(mapping__isnull=False).select_related("mapping").order_by("-start_offset")
    for span in spans:
        text = text[:span.start_offset] + span.mapping.placeholder + text[span.end_offset:]
    return text


def mask_matches(record: Request, mask) -> bool:
    """DB 의 구간·대응표로 다시 만든 전송문과 대응표가 모델 서비스가 가린 결과(/analyze 응답의 mask)와 같은지 본다.
    다르면 저장하지 못했거나 버린 구간이 있다. 그 값은 전송문에 원문으로 남고 대응표에 없어 모델 서비스의 유출 검사도 잡지 못한다."""
    if not isinstance(mask, dict):
        return False
    return build_masked_text(record) == mask.get("masked") and mapping_dict(record) == mask.get("mapping")


def run_external(request_id):
    """승인된 의뢰(queued/approved)만 processing 으로 가져가 가림 본문을 외부로 보내고 복원된 답변을 저장한다."""
    now = timezone.now()
    record = claim(
        request_id,
        filters={
            "state": S.QUEUED, "approval_status": Request.ApprovalStatus.APPROVED,
            "processing_route": Request.ProcessingRoute.EXTERNAL, "external_send_started_at__isnull": True,
        },
        state=S.PROCESSING, external_send_started_at=now, delivery_status=Request.DeliveryStatus.UNKNOWN,
    )
    if record is None:
        return None
    if CODE_SPAN_SKIPPED in (record.warning_codes or []):
        # 저장하지 못한 구간의 원문이 전송문에 남는다. 검사 단계에서 실패로 끝났어야 하는 의뢰라 보내지 않는다.
        record_delivery(record.pk, Request.DeliveryStatus.NOT_SENT)
        return apply_result(record.pk, S.PROCESSING, lambda fresh: fail(fresh, "external_send", CODE_SPAN_SKIPPED))
    masked = build_masked_text(record)
    mapping = mapping_dict(record)
    try:
        response = model_client().answer_external(masked=masked, mapping=mapping, request_id=str(record.pk))
    except ModelServiceError as exc:
        log.warning("request %s: /answer_external failed: %s", record.pk, exc)
        if isinstance(exc, NOT_SENT_ERRORS):
            record_delivery(record.pk, Request.DeliveryStatus.NOT_SENT)
        code = error_code_for(exc)
        return apply_result(record.pk, S.PROCESSING, lambda fresh: fail(fresh, "external_send", code))
    record_delivery(record.pk, Request.DeliveryStatus.CONFIRMED_SENT)

    def save_answer(fresh):
        if response.get("refusal") or response.get("stop_reason") == "refusal":
            fail(fresh, "external_send", "EXTERNAL_REFUSAL")
        elif response.get("unresolved"):
            fail(fresh, "restore", "RESTORE_INCOMPLETE")
        else:
            complete(fresh, response.get("restored"), response.get("truncated"), "external_send")

    return apply_result(record.pk, S.PROCESSING, save_answer)


# ---------- 정기 작업 ----------

def dispatch_pending(limit: int = 20) -> int:
    """queued 의뢰를 가져가 처리한다(승인 전은 검사, 승인 뒤는 외부 전송). 서버 재시작으로 스레드가 사라진 경우의 복구 경로.
    의뢰 하나가 예상 밖 오류로 끝나도 실패로 남기고 다음 의뢰로 넘어간다. 반환은 오류 없이 돌린 건수."""
    ran = 0
    rows = Request.objects.filter(state=S.QUEUED).order_by("updated_at").values_list("pk", "approval_status")[:limit]
    for pk, approval_status in list(rows):
        if approval_status is None:
            worker = run_analysis
        elif approval_status == Request.ApprovalStatus.APPROVED:
            worker = run_external
        else:
            continue
        ran += run_safely(worker, pk)
    return ran


def fail_stuck(minutes=None) -> int:
    """검사 중·처리 중에 너무 오래 머문 의뢰를 실패로 돌린다. 모델 호출 시간 초과(최대 4분)보다 길게 잡는다.
    잠금 아래 다시 확인하므로, 대상을 고른 뒤 작업자가 끝낸 의뢰는 건드리지 않는다."""
    minutes = minutes if minutes is not None else getattr(settings, "GATEWAY_STUCK_MINUTES", 15)
    stuck = {"state__in": (S.ANALYZING, S.PROCESSING), "updated_at__lt": timezone.now() - timedelta(minutes=minutes)}
    count = 0
    for pk in list(Request.objects.filter(**stuck).values_list("pk", flat=True)):
        with transaction.atomic():
            record = locked(pk, **stuck)
            if record is not None:
                fail(record, "system", "STUCK")
                count += 1
    return count
