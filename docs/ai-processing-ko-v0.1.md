# AI 처리 연결(검사·저장·승인 뒤 전송) 명세 초안 v0.1

TK-62(접수)·TK-63(승인)·TK-64(이력·답변)·TK-65(관리자) API 위에 "AI 저장자" 몫을 채운다.
접수·승인·조회 API 와 DB 모델은 바꾸지 않았고, 아래 파일을 더했다. 실행 결과는 PR 본문에 적는다.

| 파일 | 역할 |
| --- | --- |
| `gateway/processing.py` | 처리 시작, 검사 결과 저장, 사내 답변, 승인 뒤 외부 전송·복원, 정기 정리 |
| `gateway/processing_views.py` | `POST /api/v1/requests/{id}/start/` |
| `gateway/model_client.py` | 모델 서비스(AI 저장소 FastAPI) HTTP 클라이언트, 표준 라이브러리만 사용 |
| `gateway/pdf.py` | 비공개 저장소의 PDF 원본에서 본문 추출(pymupdf, AI 저장소 `src/extract.py` 와 같은 규칙) |
| `gateway/management/commands/process_requests.py` | queued 의뢰 처리·기한 만료·장시간 진행 정리(복구용, 또는 유일한 작업자) |
| `scripts/check_processing_flow.py` | 실행 중인 서버에서 접수→시작→검사→승인→전송→답변 조회 확인 |

기존 파일 변경: `views.py` 두 곳(`processing_available` 계산, 승인 뒤 전송 예약), `urls.py` 경로 1줄,
`settings.py` 설정 6줄, `requirements.txt` 에 `pymupdf==2.0`, `test_intake_api.py` 와 `check_deployed_intake_api.py` 의
`processing_available` 기대값 1줄씩(의뢰문이 있는 준비 중 의뢰는 이제 `true`).

## 1. 상태 흐름

```
preparing ──start──▶ queued ──검사──▶ analyzing ─┬─ external ─▶ awaiting_approval ─승인─▶ queued(approved) ─전송─▶ processing ─▶ completed
                                               │                                 └거절─▶ rejected        └기한─▶ expired
                                               ├─ local ─▶ processing ─사내 답변─▶ completed
                                               ├─ block ─▶ blocked
                                               └─ 불완전 검사·호출 실패·답변 문제 ─▶ failed (error_stage / error_code)
```

- 처리 시작 API 가 PDF 본문을 뽑아 `input_text` 를 고정하고 `queued` 로 바꾼다. 검사는 배경에서 돈다.
- 외부 경로는 구간 0건이어도 `prepare_approval`(TK-63)로 승인 대기(30분)에 들어간다.
- 승인 API 는 기존대로 `queued`/`approved` 만 기록한다. 그 직후 AI 작업자가 같은 의뢰를 `processing` 으로 가져가 전송한다.
- 불완전 검사(`degraded`)는 즉시 탐지 결과를 남기고 `failed`/`INSPECTION_DEGRADED` 로 끝낸다. 사내 답변도 만들지 않는다.
- 구간을 하나라도 저장하지 못하면 `failed`/`SPAN_SAVE_SKIPPED`, 저장한 구간으로 다시 만든 전송문·대응표가 모델 서비스의 `mask` 와 다르면
  `failed`/`MASK_MISMATCH` 로 끝낸다. 둘 다 가려지지 않은 원문이 전송문에 남는 경우라 승인 대기로 보내지 않는다(불완전 검사와 같은 취급).
- 답변이 생성 상한에서 잘리거나 비어 있으면 `failed`(`ANSWER_TRUNCATED`/`ANSWER_EMPTY`). 복원되지 않은 자리표가 남으면 `failed`/`RESTORE_INCOMPLETE`.
- 실패는 새 의뢰로 다시 접수한다. 같은 의뢰를 다시 시작할 수 없다.
- 작업자는 모델 호출 뒤 행을 다시 잠가, 아직 자기가 가져간 상태일 때만 결과를 쓴다. 그 사이 `failed`(`STUCK` 등)로 끝난 의뢰에 늦게 온
  결과는 버리고 실패 기록을 유지한다. 숨김(`hidden_at`)처럼 처리 중에 다른 쪽이 바꾼 값은 덮어쓰지 않는다.

## 2. 처리 시작 API

`POST /api/v1/requests/{request_id}/start/` — 활성 일반 이용자 본인, 세션 Cookie + `X-CSRFToken`, 본문 없음(또는 `{}`).
의뢰문 수정은 기존 PATCH 로 먼저 한다.

성공 `202 Accepted`, 본문은 접수 응답과 같은 `{"request": {...}}` 이고 `state` 는 `queued`, 문서가 있으면
`document.extraction_status` 는 `ready`. 이후 상태는 상세 조회(GET)로 본다. 접수·상세 응답의 `processing_available` 은
준비 중이고 의뢰문이 있으며 문서가 `pending`/`ready` 일 때 `true` 다.

| HTTP | 코드 | 의미 |
| --- | --- | --- |
| 400 | INSTRUCTION_REQUIRED | 의뢰문 없음(PDF 선접수) |
| 400 | INPUT_TOO_LONG | 의뢰문 + 두 개 LF + 본문이 60,000 코드포인트 초과 |
| 400 | PDF_NO_TEXT / INVALID_PDF / PDF_ENCRYPTED / PDF_PAGE_LIMIT | 추출 실패. 의뢰와 문서를 `failed` 로 남긴다 |
| 400 | INVALID_INPUT | 본문·조회 조건을 보냄 |
| 403 | FORBIDDEN / CSRF_FAILED / AUTH_REQUIRED | 관리자·권한·CSRF·미로그인 |
| 404 | REQUEST_NOT_FOUND | 남의 의뢰, 숨긴 의뢰 |
| 409 | PROCESSING_ALREADY_STARTED | 준비 중이 아님 |
| 409 | REQUEST_NOT_EDITABLE / DOCUMENT_NOT_READY / DOCUMENT_NOT_AVAILABLE | 추출 실패 문서, 업로드 중, 저장 객체 없음 |
| 503 | DOCUMENT_UNAVAILABLE / REQUEST_UPDATE_FAILED | 원본 읽기·저장 실패 |

## 3. 저장 규칙(DB)

- `requests.input_text` = 의뢰문 + `"\n\n"` + 추출 본문(PDF 없으면 의뢰문 그대로). 정규화 없음. 모델 서비스에는 이 문자열을 그대로 보낸다.
- `detection_spans.start_offset/end_offset` 은 `input_text` 의 Python 코드포인트 기준, 끝 미포함. `input_text[start:end] == sensitive_terms.value`.
  `input_source` 는 본문 시작 위치(`len(instruction) + 2`) 앞이면 `instruction`, 뒤면 `pdf`.
- 가린 구간은 `masking_mappings(placeholder)` 에 연결된다. 자리표 형식은 모델 서비스가 정한다(`[[ORG_1]]`, `[[PERSON_1]]`, `[[ID_1]]`, `[[AMT_1]]`, `[[CONTACT_1]]`, `[[PROJECT_1]]`).
  같은 값은 한 의뢰 안에서 같은 자리표를 쓴다. 인명은 항상 가린다.
- 모델 라벨 7종(company/project/doc_no/price/person/contact/biz_no)만 저장한다. 그 밖의 라벨 구간은 버리고 `SPAN_LABEL_UNSUPPORTED`,
  겹침 등 제약 위반으로 못 넣은 구간은 `SPAN_SAVE_SKIPPED` 를 `warning_codes` 에 남긴다. 못 넣은 구간이 있으면 의뢰는 실패로 끝난다.
  버려지는 라벨은 현재 모델 서비스가 가리지 않는 `other` 뿐이다. 모델 서비스가 가린 구간이 버려지면 아래 일치 확인에서 실패한다.
- 외부 경로는 저장 직후 `build_masked_text` 결과와 대응표가 `/analyze` 응답의 `mask.masked`·`mask.mapping` 과 같은지 확인한다.
  다르거나 응답에 `mask` 가 없으면 `MASK_MISMATCH` 로 실패한다.
- `warning_codes`: `INJECTION_DETECTED`, `LLM_FAILED`, `LLM_OUTPUT_TRUNCATED`, `ROUTE_FAILED`, `ROUTE_INPUT_TRUNCATED`(경로 판정이 앞 6,000자만 봄),
  `LLM_SPANS_DROPPED`, `SPAN_LABEL_UNSUPPORTED`, `SPAN_SAVE_SKIPPED`, `WARNING_OTHER`. 모델 서비스의 한국어 문장은 저장하지 않는다.
- `inspection_versions`: 모델 서비스 `versions` + `mode`(`full`/`degraded`).
- `error_stage`/`error_code`: `extraction`/PDF_* · `inspection`/MODEL_UNAVAILABLE·MODEL_AUTH·MODEL_INPUT·MODEL_SERVER_ERROR·INSPECTION_DEGRADED·SPAN_SAVE_SKIPPED·MASK_MISMATCH ·
  `decision`/DECISION_UNKNOWN · `local_answer`/ANSWER_TRUNCATED·ANSWER_EMPTY·MODEL_* · `external_send`/LEAK_BLOCKED·EXTERNAL_RATE_LIMITED·EXTERNAL_REFUSAL·ANSWER_TRUNCATED·ANSWER_EMPTY·SPAN_SAVE_SKIPPED·MODEL_* ·
  `restore`/RESTORE_INCOMPLETE · `approval`/APPROVAL_NOT_AVAILABLE·PREVIEW_NOT_AVAILABLE · `system`/INTERNAL_ERROR·STUCK.
- `delivery_status`: 외부 경로 결정 시 `not_sent`, 전송 시작 시 `unknown`, 모델 서비스가 응답하면 `confirmed_sent`.
  모델 서비스 안에서 거부되어 밖으로 안 나간 경우(401·409 유출 검사·422)는 `not_sent` 로 되돌린다.
  응답을 기다리는 사이 의뢰가 `failed` 로 정리됐어도 이 값은 사실대로 갱신한다(상태와 실패 사유는 그대로 둔다).
- 전송문 전문은 저장하지 않는다. 승인 뒤 `input_text` 와 저장된 구간·대응표로 다시 만들어 보낸다(`build_masked_text`).
- `answer_text` 는 복원이 끝난 최종 답변만, `completed` 와 함께 저장한다.

## 4. 실행 방식과 설정

| 설정 | 값 |
| --- | --- |
| `MODEL_SERVICE_URL` | 모델 서비스 주소(예: `http://100.x.x.x:8010`, Tailscale). 비어 있으면 검사가 `MODEL_UNAVAILABLE` 로 실패 |
| `MODEL_API_KEY` | 모델 서비스 `X-API-Key`. AI 담당자가 서버용 키를 따로 발급해 `.env` 에 넣는다 |
| `GATEWAY_WORKER` | `thread`(기본): 요청 처리 프로세스의 배경 스레드. 다른 값: `manage.py process_requests --loop` 만 처리 |
| `GATEWAY_LOCAL_MAX_TOKENS` | 사내 답변 생성 상한(1024) |
| `GATEWAY_STUCK_MINUTES` | 검사 중·처리 중이 이보다 오래가면 `process_requests` 가 `STUCK` 실패 처리(15) |

- `thread` 는 Django 프로세스 하나를 전제한다(현재 `runserver` 컨테이너). 프로세스가 여럿이면 30B 직렬화 잠금이 프로세스별이 되고,
  재시작 시 진행 중 스레드가 사라진다. 사라진 의뢰는 `queued` 로 남아 `process_requests` 가 가져간다(`analyzing`/`processing` 은 `STUCK`).
- 30B·외부 LLM 호출 시간: 검사 10~100초(문서 길이), 사내 답변 수십 초, 외부 전송 5초 안팎. 모델 서비스는 30B 호출을 직렬 처리한다.
- `python manage.py process_requests [--loop --interval 5]` 를 `expire_approvals` 와 함께 정기 실행하면 만료·복구·정리가 한 번에 된다.
  의뢰 하나가 예상 밖 오류로 끝나면 `failed`/`INTERNAL_ERROR` 로 남기고 다음 의뢰를 처리한다(배경 스레드와 같은 보호).
- 테스트는 실제 AI 를 부르지 않는다(`GATEWAY_WORKER=command` 와 가짜 클라이언트). `gateway/testdata/*.json` 은 가상 문서(D2~D4)로 받은 실제 응답 형식이다.

## 5. TK-62 문서 9절 질문에 대한 답

1. AI 저장소에는 추출 함수·모델 서비스만 있고 접수·파일 저장·DB 저장은 이 저장소(이 변경)가 한다.
2. 추출 입력은 바이트다. 비공개 저장소에서 `storage_object_key` 로 읽는 담당은 `processing.document_text`(이 변경).
3. 반환은 본문 문자열·쪽수. 글자 없음(`PDF_NO_TEXT`)·암호화·쪽수 초과·손상은 코드로 구분한다. 부분 실패·시간 초과 구분은 없다(pymupdf 는 쪽 단위로 뽑고 실패하면 예외).
4. 추출은 처리 시작 API 안에서 동기(20쪽 기준 1초 미만). 검사·답변은 배경(위 4절). 상태 갱신·DB 저장은 모두 `processing.py`.
5. 결합 규칙은 `의뢰문 + "\n\n" + 본문`, 상한 60,000 코드포인트(Python `len`). 시작 시 검증하고 `input_text` 로 고정한다.
6. 분류는 라벨 7종, 기준 문자열은 `sensitive_terms.value`, 위치는 3절. 모델 서비스가 겹침을 정리해 준다. 그래도 남는 겹침은 저장 실패로 보고 의뢰를 실패 처리한다.
7. 자리표·대응표 형식은 3절. 저장 시점은 검사 직후(승인 전). 승인 뒤 전송과 복원은 모델 서비스 `/answer_external` 이 한 번에 한다.
8. 불완전 검사·생성 실패·응답 유실·복원 실패는 3절의 `error_code` 와 `delivery_status`. 중복 실행은 행 잠금 + 상태 조건(`claim`)으로 막고, 결과 저장도 같은 방식으로 한다(`apply_result`).

## 6. 남은 사항

- 배포 저장소(Cloud Storage) 연결 시 `document_storage().open` 이 그대로 동작하는지 확인.
- 서버에 `MODEL_SERVICE_URL`·`MODEL_API_KEY` 를 넣고 `requirements.txt` 변경으로 이미지를 다시 빌드해야 한다.
- 승인 대기 중 의뢰문·구간 변경은 지원하지 않는다(요구사항대로 인명 토글 없음).
- FE: 접수 뒤 `processing_available` 이 `true` 면 시작 버튼, 시작 뒤에는 상세 조회로 상태를 보여 준다. `awaiting_approval` 이면 사전 확인·승인 API.
