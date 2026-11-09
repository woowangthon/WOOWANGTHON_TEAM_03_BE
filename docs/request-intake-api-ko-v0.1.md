# PDF·의뢰 접수 API 명세 초안 v0.1 — TK-62

팀 내부 FE 연동용. 사용자 실행 보고로 접수 API·동시 접수 테스트와 accounts·gateway 전체 테스트의 OK 및 로컬 마이그레이션 적용을 확인했다. 실제 PDF의 브라우저 수동 확인, 서버 배포·FE 통합 검증은 아직 완료로 확인되지 않았다.

## 1. 이번 구현 범위

- PDF+의뢰문, 의뢰문 단독, PDF 선접수 지원.
- 의뢰 ID 발급, 로그인 사용자 연결, 중복 접수 방지, 비공개 원본 저장.
- 준비 중 의뢰문 수정, 본인의 접수 상태 조회, 본인·관리자의 PDF 원본 조회.
- 텍스트 추출·검사·AI 호출·외부 전송·승인·답변 생성은 아직 연결하지 않는다.
- 접수 직후 요청은 `preparing`, PDF 추출 상태는 `pending`이다. `pending`은 저장 완료 후 추출 대기이며 추출 완료가 아니다.
- AI 미연결 상태에서 `processing_available`은 항상 `false`이다. 처리 시작 버튼을 활성화하는 근거로 사용하지 않는다.
- 이력 목록·숨김·답변 조회는 TK-64, 관리자 감사 조회는 TK-65의 별도 API다.

## 2. 공통 규칙

- 기존 세션 Cookie 인증을 사용한다. POST·PATCH에는 `X-CSRFToken`을 포함한다.
- JSON 요청은 `Content-Type: application/json`, PDF 포함 요청은 `multipart/form-data`로 보낸다.
- FE의 `FormData` 전송에서는 Content-Type을 직접 지정하지 않는다. 브라우저가 boundary를 설정하도록 한다.
- 공개 가입·임의 사용자 지정은 지원하지 않는다. 소유자는 로그인 세션에서 결정한다.
- 응답의 파일 URL은 권한 확인 API의 상대 경로다. 공개 스토리지 URL이 아니다.
- 요청문·PDF·응답은 FE 로그나 브라우저 영구 저장소에 저장하지 않는다.

### 이번 코드의 입력 계산 기준 — FE와 경계값 확인 필요

| 항목 | 구현 기준 |
| --- | --- |
| PDF 수 | 요청당 1개 |
| PDF 크기 | 최대 10,000,000바이트(10MB), 경계값 포함 |
| PDF 페이지 | 1~20쪽, 암호화 PDF 거부 |
| 파일 이름 | `.pdf` 확장자, 제어 문자·경로 구분자 거부 |
| 의뢰문 | 최대 4,000 Unicode 코드포인트. 공백·개행도 포함 |
| 정규화 | 앞뒤 공백 제거·개행 변경·Unicode 정규화 없음 |
| JSON 본문 | 최대 65,536바이트 |
| 검사 입력 전체 | 최대 60,000코드포인트. 추출 완료 문서의 의뢰문 수정 시 `의뢰문 + 두 개의 LF 개행 + 추출 본문`으로 검사 |

JS의 `text.length`는 UTF-16 단위이므로 BE 계산과 다를 수 있다. 코드포인트 수는 `[...text].length`로 맞춘다. 예: `😀`는 1, `가\n😀`는 3이다. 이 계산은 화면상의 글자 덩어리 수와 다르다.

PDF 구조·페이지 검증 통과는 텍스트 추출 가능 또는 보안 검사 통과를 뜻하지 않는다. 스캔·빈 문서·부분 추출 실패 판정과 최종 합계 상한 검사는 추출/처리 시작 연결 시 추가 검증한다. 두 개 LF 구분자는 현재 구현안이며 AI와 검사 문자열을 통일하기 전에는 검출 좌표 계약으로 확정하지 않는다.

## 3. API 목록

| 메서드 | 경로 | 권한·목적 |
| --- | --- | --- |
| POST | `/api/v1/requests/` | 활성 사용자·관리자 본인의 의뢰 접수 |
| GET | `/api/v1/requests/{request_id}/` | 본인 접수 상태·입력·파일 메타데이터 조회 |
| PATCH | `/api/v1/requests/{request_id}/` | 본인의 준비 중 의뢰문 추가·수정 |
| GET | `/api/v1/requests/{request_id}/file/` | 본인 또는 서비스 관리자의 PDF 원본 열기 |

관리자 전체 감사 상세 API는 별도다. 위의 본인용 GET·PATCH에 관리자 권한으로 타인의 요청을 지정해도 404로 거부한다. PDF 원본 API에만 관리자 전체 원본 조회 권한을 적용한다.

## 4. 접수

입력 필드:

| 필드 | 형식 | 규칙 |
| --- | --- | --- |
| client_request_key | UUID 문자열 | FE가 새 접수 동작마다 생성. 동일 접수 재전송에는 같은 값 사용 |
| instruction | 문자열 | PDF가 없으면 필수·공백만 입력 불가. PDF 선접수에서는 생략 또는 빈 문자열 허용 |
| pdf | 파일 | 선택. 파일을 보내려면 multipart 사용 |

사용자 ID·상태·저장 경로·AI 결과 등 추가 필드는 거부한다. multipart에서 같은 필드의 중복 값·여러 PDF를 거부한다.

의뢰문 단독 예시(모두 가상 데이터):

```json
{
  "client_request_key": "550e8400-e29b-41d4-a716-446655440000",
  "instruction": "가상 문서의 작성 기준을 알려 주세요."
}
```

PDF 선접수는 FormData에 `client_request_key`와 `pdf`를 넣는다. 의뢰문을 같이 받으면 `instruction`도 추가한다.

새 접수 성공: HTTP 201. 예시의 파일 크기와 날짜는 설명용이다.

```json
{
  "request": {
    "request_id": "8fa4463a-a727-4d54-af0e-14104f4c8899",
    "client_request_key": "550e8400-e29b-41d4-a716-446655440000",
    "instruction": "",
    "state": "preparing",
    "created_at": "2026-11-09T00:00:00+00:00",
    "updated_at": "2026-11-09T00:00:00+00:00",
    "processing_available": false,
    "document": {
      "original_filename": "fictional.pdf",
      "size_bytes": 1200,
      "page_count": 1,
      "extraction_status": "pending",
      "file_url": "/api/v1/requests/8fa4463a-a727-4d54-af0e-14104f4c8899/file/"
    }
  },
  "replayed": false
}
```

PDF가 없으면 `document`는 null이다. 저장 경로·접수 지문·검사 원문·추출 본문·대응표·AI 답변은 이 응답에 포함하지 않는다.

### 중복·응답 유실

- 같은 사용자·접수 키·최초 입력(의뢰문·파일명·PDF 바이트)이 같으면 기존 의뢰를 HTTP 200, `replayed: true`로 반환한다. 다시 저장하거나 AI를 실행하지 않는다.
- 같은 키로 다른 입력을 보내면 HTTP 409 `REQUEST_KEY_CONFLICT`다.
- 의뢰문을 PATCH로 수정한 뒤 최초 접수를 재전송해도 수정 내용을 덮어쓰지 않고 현재 의뢰 정보를 반환한다.
- 서로 다른 키의 제출은 같은 PDF라도 별도 의뢰·문서로 저장한다.
- 전송 결과가 불명확할 때 새 키로 자동 재접수하지 않는다. ID를 받았다면 GET으로 조회하고, ID를 받지 못했다면 최초 키·최초 입력으로 접수 결과를 확인할 수 있다.
- 준비 중 PDF 교체는 지원하지 않는다. 다른 PDF는 새 키로 접수한다.
- 기존 데이터에 접수 지문이 없거나 같은 키의 요청이 숨김 상태이면 409로 반환한다.

## 5. 준비 중 의뢰문 수정과 조회

PATCH JSON:

```json
{"instruction": "이 PDF를 요약해 주세요."}
```

의뢰문은 비어 있지 않아야 한다. 본인 요청의 `preparing` 상태에서만 수정할 수 있다. PDF 추출 실패 또는 처리 시작 이후에는 409로 거부한다. 같은 의뢰문으로 재전송하면 새 의뢰를 만들거나 수정 시각을 바꾸지 않는다. ID·최초 접수 시각·PDF·최초 접수 지문은 유지한다.

GET과 PATCH 성공은 HTTP 200, `{"request": ...}`이다. 위 접수 응답의 request 구조를 사용하며 `replayed`는 없다. GET은 현재 저장된 상태만 조회하고 처리를 다시 실행하지 않는다.

향후 처리 상태 값: `preparing`, `queued`, `analyzing`, `awaiting_approval`, `processing`, `completed`, `blocked`, `rejected`, `expired`, `failed`. 이번 접수 API는 새 요청을 `preparing`으로만 생성한다.

문서 상태 값: `uploading`, `pending`, `extracting`, `ready`, `failed`. 이번 접수 성공 결과는 `pending`이다. 다음 작업에서 실제 추출을 연결하기 전까지 `extracting` 또는 `ready`로 임의 갱신하지 않는다.

## 6. PDF 원본 열기

- 성공: HTTP 200, `Content-Type: application/pdf`, `Content-Disposition: inline`.
- 로그인 Cookie가 필요하다. 본인 외 일반 사용자는 404, 관리자에게는 전체 요청의 원본 조회를 허용한다.
- 본인 목록에서 숨긴 요청은 본인용 조회·원본 API에서 제외한다. 관리자 원본 조회는 유지한다.
- 브라우저는 같은 출처의 API 또는 FE 개발 프록시로 접근한다. iframe은 SAMEORIGIN 기준이다.
- PDF 요청에서 `Accept: application/pdf`도 사용할 수 있다. 실패 응답은 JSON으로 반환하므로 HTTP 상태·Content-Type을 먼저 확인한다.
- 캐시 금지 헤더를 포함한다. 웹 열기를 지원하며 브라우저의 저장·복사를 완전히 차단하는 기능은 아니다.

## 7. 주요 오류

공통 형태: `{"error":{"code":"...","message":"..."}}`. 입력 검증 오류에는 `fields`가 추가될 수 있다. 실제 입력·비밀번호·내부 저장 경로·예외 상세는 반환하지 않는다.

| HTTP | 코드 | 의미 |
| --- | --- | --- |
| 400 | INVALID_INPUT | 누락·형식·미정의 필드·중복 multipart 항목·복수 파일 |
| 400 | INVALID_JSON | JSON 파싱 실패 |
| 400 | INVALID_PDF | PDF 형식·파일명·구조 오류 또는 페이지 없음 |
| 400 | PDF_ENCRYPTED | 암호화 PDF |
| 400 | PDF_PAGE_LIMIT | 20쪽 초과 |
| 400 | INPUT_TOO_LONG | 추출 완료 본문과 의뢰문 합계 초과 |
| 403 | AUTH_REQUIRED / FORBIDDEN / CSRF_FAILED | 인증·권한·요청 보호 오류 |
| 404 | REQUEST_NOT_FOUND / DOCUMENT_NOT_FOUND | 대상 없음 또는 조회 권한 없음 |
| 409 | REQUEST_KEY_CONFLICT | 같은 키의 최초 입력 불일치 또는 기존 접수 지문 없음 |
| 409 | REQUEST_NOT_AVAILABLE | 숨긴 요청에 대한 접수 재전송 |
| 409 | REQUEST_NOT_EDITABLE | 처리 시작·종료·추출 실패 후 수정 |
| 409 | DOCUMENT_NOT_READY / DOCUMENT_NOT_AVAILABLE | 필요한 추출 본문·저장 객체 정보 없음 |
| 413 | PDF_TOO_LARGE / INPUT_TOO_LARGE | 파일 또는 요청 본문 크기 초과 |
| 405 / 415 | METHOD_NOT_ALLOWED / UNSUPPORTED_MEDIA_TYPE | 메서드·요청 형식 오류 |
| 503 | INTAKE_SAVE_FAILED | DB·파일 접수 저장 실패 |
| 503 | REQUEST_UPDATE_FAILED / REQUEST_READ_FAILED | 의뢰 수정·조회 실패 |
| 503 | DOCUMENT_UNAVAILABLE | 원본 파일 읽기 실패 |

잘못된 UUID 경로, 미들웨어·웹서버의 크기 제한, 예상 밖 서버 응답은 위 JSON 형식이 아닐 수 있다. FE는 JSON이 아닌 오류도 처리한다. 접수 검증에 실패한 입력은 요청·문서로 등록하지 않는다. 저장 실패 시 DB는 롤백하고 참조되지 않은 파일을 정리한다. DB 커밋 여부가 불명확하거나 정리 자체가 실패하면 파일을 보존하고 점검 로그를 남긴다.

## 8. DB·저장소 변경과 로컬 확인

- requests에 최초 접수 지문 `intake_fingerprint` 추가. 원문 복원이나 암호화 용도가 아닌 중복 접수 비교용 SHA-256이다.
- documents.extraction_status에 `pending` 추가. 기존 상태·기존 데이터는 변경하지 않는다.
- 마이그레이션 `0002_request_intake`를 추가했다. 사용자 실행 보고로 로컬 적용 및 `[X] 0002_request_intake` 표시를 확인했다. 서버에는 아직 적용하지 않았다.
- 개발 환경은 Docker 전용 `private_documents` 볼륨을 사용한다. PDF는 DB·Git·Docker 이미지에 넣지 않는다.
- **요구사항의 배포 저장소는 비공개 Cloud Storage다. 이번 로컬 파일 저장소는 개발용이며 Cloud Storage 연결 완료를 뜻하지 않는다.** `gateway/storage.py`를 연결 지점으로 두고 배포 전에 버킷·권한·저장 백엔드·원본 이전 여부를 확인한다.
- PDF 구조·페이지 검증용으로 `pypdf==7.0.0`을 추가했다. AI 담당자의 추출 함수를 대체하지 않는다. 참고: https://pypi.org/project/pypdf/7.0.0/

로컬 저장소 루트에서 순서대로 실행한다. 실패하면 이후 단계로 진행하지 않고 오류를 확인한다.

```bash
docker compose up --build -d
docker compose exec backend python manage.py check
docker compose exec backend python manage.py makemigrations --check --dry-run
docker compose exec backend python manage.py test accounts gateway -v 2
docker compose exec backend python manage.py migrate --plan
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py showmigrations gateway
```

예상 결과: 시스템 검사 이상 없음, 새 마이그레이션 생성 필요 없음, 테스트 OK, 적용 계획에 gateway.0002_request_intake, 적용 후 `[X] 0002_request_intake`. 다른 미적용 마이그레이션이 있으면 함께 확인한다. 테스트는 별도 테스트 DB·임시 파일 저장소를 사용한다.

실제 PDF 수동 확인: 로그인 후 PDF 접수 → 201/preparing/pending → 같은 키·파일 재전송 200/replayed → 동일 의뢰에 PATCH로 의뢰문 추가 → GET 조회 → 본인·관리자의 원본 열기 → 다른 일반 사용자 404. 실제 브라우저의 PDF 표시와 서버 성능은 별도 확인한다.

## 9. AI 담당자에게 한 번에 확인할 목록

1. 기존 코드의 위치와 범위: 추출 함수뿐인지, 접수 API·파일 저장·DB 저장까지 포함하는지.
2. 추출 함수의 입력: 파일 객체·바이트·임시 경로 중 어떤 형식인지. 비공개 Cloud Storage 파일을 읽는 담당은 누구인지.
3. 반환 데이터: 추출 본문, 페이지 정보, 정상/부분 실패/스캔/암호화/시간 초과의 구분 방식.
4. 추출 실행 방식: 동기·비동기 여부, 제한 시간과 동시에 실행할 작업 수. 요청 상태를 갱신하고 결과를 DB에 저장하는 담당.
5. 검사 입력: 요청문과 추출 본문 결합 규칙, 60,000문자 및 코드포인트 기준 수용 여부. 시작 시 최종 입력 검증과 고정 방식.
6. 검출 결과: 분류 코드, 기준 문자열, 시작·끝 위치 규칙, 겹치는 검출의 정리 방식.
7. 마스킹·복원: 자리표·원문 대응 형식, DB 저장 시점, 승인 후 실제 마스킹 및 답변 복원 연결 방식.
8. 검사 불완전·답변 생성 실패·외부 응답 유실·복원 실패의 반환 형식과 중복 실행 방지 방식.

이 확인 전에도 접수·저장·권한 API는 로컬에서 검증할 수 있다. TK-62 전체 완료와 AI 연결·Cloud Storage 연결·서버 배포 완료는 각각 구분한다.
