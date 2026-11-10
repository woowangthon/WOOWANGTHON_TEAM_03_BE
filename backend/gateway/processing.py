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
  겹침 등 제약 위반 구간은 건너뛰고 경고 코드(SPAN_SAVE_SKIPPED)만 남긴다. 저장 실패를 정상으로 보이게 하지 않는다.
- 외부 전송 승인 기록은 approval_decision.decide_approval(queued/approved) 이 하고, 실제 전송은 run_external 이 한다.
  전송문은 저장하지 않고 승인 뒤 DB 의 구간·대응표로 다시 만든다(build_masked_text).
- 정책(BE 요구사항 v1.0): 인명 항상 가림, 구간 0건도 외부 전송 전 승인, 불완전 검사(degraded)는 실패·전송 금지,
  답변 절단·복원 불완전은 실패. 실패는 failed + error_stage/error_code 로 남기고 재시도는 새 의뢰다.
- 배경 실행: GATEWAY_WORKER=thread 면 커밋 뒤 배경 스레드, 아니면 `manage.py process_requests` 가 queued 를 가져간다.
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
CODE_SPAN_SKIPPED = "SPAN_SAVE_SKIPPED"                # 제약 위반(겹침 등)으로 구간을 저장하지 못함
ERROR_CODES = (
    (AuthError, "MODEL_AUTH"), (InputError, "MODEL_INPUT"), (LeakBlocked, "LEAK_BLOCKED"),
    (RateLimited, "EXTERNAL_RATE_LIMITED"), (Unavailable, "MODEL_UNAVAILABLE"), (ServerError, "MODEL_SERVER_ERROR"),
)
# 외부 전송 요청이 모델 서비스 안에서 거부되어 외부로 나가지 않은 경우(delivery_status=not_sent 로 확정)
NOT_SENT_ERRORS = (AuthError, InputError, LeakBlocked)


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
    url = getattr(settings, "MODEL_SERVICE_URL", "")
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
    """실패를 failed + error_stage/error_code 로 남긴다. 원문·답변·구간은 지우지 않는다."""
    record.state = S.FAILED
    record.error_stage = stage[:64]
    record.error_code = code[:100]
    if inspection_failed:
        record.inspection_status = Request.InspectionStatus.FAILED
    record.finished_at = timezone.now()
    record.save()
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


def run_guarded(worker, request_id) -> None:
    """스레드 본체. 예상 밖 예외는 failed 로 남기고 스레드의 DB 연결을 닫는다."""
    try:
        worker(request_id)
    except Exception:  # noqa: BLE001 - 배경 스레드는 예외를 올릴 곳이 없다
        log.exception("request %s: worker %s crashed", request_id, worker.__name__)
        try:
            record = Request.objects.filter(pk=request_id, state__in=(S.QUEUED, S.ANALYZING, S.PROCESSING)).first()
            if record is not None:
                fail(record, "system", "INTERNAL_ERROR")
        except Exception:  # noqa: BLE001
            log.exception("request %s: could not record the failure", request_id)
    finally:
        connections.close_all()


def claim(request_id, *, filters: dict, **changes):
    """잠금 아래 조건을 확인하고 상태를 바꾼다. 조건에 안 맞으면(다른 작업자가 먼저 가져갔거나 상태가 바뀜) None."""
    with transaction.atomic():
        record = Request.objects.select_for_update().filter(pk=request_id, **filters).first()
        if record is None:
            return None
        for name, value in changes.items():
            setattr(record, name, value)
        record.save()
    return record


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
        return fail(record, "inspection", error_code_for(exc), inspection_failed=True)
    body_start = len(record.instruction) + len(BODY_SEP) if document is not None else None
    return apply_analysis(record, response, body_start)


def apply_analysis(record: Request, response: dict, body_start) -> Request:
    """/analyze 응답을 저장하고 다음 단계로 넘긴다. body_start 는 input_text 에서 PDF 본문이 시작하는 위치(PDF 없으면 None)."""
    mode = response.get("mode") or ""
    decision = response.get("decision") or {}
    route = response.get("route") or {}
    Route = Request.ProcessingRoute
    with transaction.atomic():
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
        now = timezone.now()
        if action == "block":
            record.processing_route = Route.BLOCKED
            record.state, record.finished_at = S.BLOCKED, now
        elif not complete:
            # 불완전 검사는 결과를 남기되 실패로 끝낸다. 외부 전송은 물론 사내 답변도 만들지 않는다.
            record.processing_route = {"local": Route.LOCAL, "external": Route.EXTERNAL}.get(action)
            record.state, record.finished_at = S.FAILED, now
            record.error_stage, record.error_code = "inspection", "INSPECTION_DEGRADED"
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
        record.save()
    log.info("request %s analyzed: mode=%s action=%s spans=%d skipped=%d unsupported=%d codes=%s",
             record.pk, mode, action, saved, skipped, unsupported, record.warning_codes)
    if record.state == S.PROCESSING:
        return run_local_answer(record)
    if record.state == S.ANALYZING and record.processing_route == Route.EXTERNAL:
        try:
            record, _ = prepare_approval(record.pk)
        except IntakeError as exc:
            log.warning("request %s: prepare_approval refused: %s", record.pk, exc.code)
            record.refresh_from_db()
            return fail(record, "approval", exc.code)
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
    """최종 답변 저장. 잘렸거나 비어 있으면 실패로 남긴다(상세 조회는 completed 에서만 답변을 낸다)."""
    if truncated:
        return fail(record, stage, "ANSWER_TRUNCATED")
    if not isinstance(answer, str) or not answer.strip():
        return fail(record, stage, "ANSWER_EMPTY")
    record.answer_text = answer
    record.state, record.finished_at = S.COMPLETED, timezone.now()
    record.save()
    log.info("request %s completed via %s", record.pk, stage)
    return record


def run_local_answer(record: Request) -> Request:
    """사내 처리: 연구실 PC 30B 가 원문으로 답한다. 외부로 나가지 않는다."""
    try:
        with MODEL_LOCK:
            response = model_client().answer_local(
                text=record.input_text, request_id=str(record.pk),
                max_tokens=getattr(settings, "GATEWAY_LOCAL_MAX_TOKENS", 1024),
            )
    except ModelServiceError as exc:
        log.warning("request %s: /answer_local failed: %s", record.pk, exc)
        return fail(record, "local_answer", error_code_for(exc))
    add_codes(record, [warning_code(text) for text in response.get("warnings") or []])
    return complete(record, response.get("answer"), response.get("truncated"), "local_answer")


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
    masked = build_masked_text(record)
    mapping = mapping_dict(record)
    try:
        response = model_client().answer_external(masked=masked, mapping=mapping, request_id=str(record.pk))
    except ModelServiceError as exc:
        log.warning("request %s: /answer_external failed: %s", record.pk, exc)
        if isinstance(exc, NOT_SENT_ERRORS):
            record.delivery_status = Request.DeliveryStatus.NOT_SENT
        return fail(record, "external_send", error_code_for(exc))
    record.delivery_status = Request.DeliveryStatus.CONFIRMED_SENT
    if response.get("refusal") or response.get("stop_reason") == "refusal":
        return fail(record, "external_send", "EXTERNAL_REFUSAL")
    if response.get("unresolved"):
        return fail(record, "restore", "RESTORE_INCOMPLETE")
    return complete(record, response.get("restored"), response.get("truncated"), "external_send")


# ---------- 정기 작업 ----------

def dispatch_pending(limit: int = 20) -> int:
    """queued 의뢰를 가져가 처리한다(승인 전은 검사, 승인 뒤는 외부 전송). 서버 재시작으로 스레드가 사라진 경우의 복구 경로."""
    ran = 0
    rows = Request.objects.filter(state=S.QUEUED).order_by("updated_at").values_list("pk", "approval_status")[:limit]
    for pk, approval_status in list(rows):
        if approval_status is None:
            run_analysis(pk)
        elif approval_status == Request.ApprovalStatus.APPROVED:
            run_external(pk)
        else:
            continue
        ran += 1
    return ran


def fail_stuck(minutes=None) -> int:
    """검사 중·처리 중에 너무 오래 머문 의뢰를 실패로 돌린다. 모델 호출 시간 초과(최대 4분)보다 길게 잡는다."""
    minutes = minutes if minutes is not None else getattr(settings, "GATEWAY_STUCK_MINUTES", 15)
    cutoff = timezone.now() - timedelta(minutes=minutes)
    count = 0
    for record in Request.objects.filter(state__in=(S.ANALYZING, S.PROCESSING), updated_at__lt=cutoff):
        fail(record, "system", "STUCK")
        count += 1
    return count
