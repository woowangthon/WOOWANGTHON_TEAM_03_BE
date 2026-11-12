# AI 교환기 동료 · 백엔드

충북대학교 AI 우왕톤 3팀 ‘초코 츄러스’의 문서·질의 처리 서비스다. Django REST Framework로 요청 접수, AI 처리 상태, 사용자 승인 및 이용 이력을 제공한다.

## 요청 처리 흐름

1. 사용자는 요청문만 제출하거나 PDF를 함께 첨부할 수 있다. PDF를 먼저 올렸다면 같은 요청에 요청문을 추가한 뒤 처리를 시작한다.
2. 처리를 시작하면 백엔드는 요청문과 PDF에서 추출한 본문을 합쳐 검사할 텍스트를 확정한다. 모델 서비스에 검사를 요청하고 검출 위치, 분류, 마스킹 대응 정보를 저장한다.
3. 사내 처리로 결정되면 내부 모델의 답변을 저장한다. 외부 AI가 필요한 경우에는 사용자가 마스킹 예정 구간을 확인하고 승인한 뒤 마스킹한 텍스트를 전송한다. 검출된 구간이 없어도 승인은 필요하며, 거절·만료·정책 차단된 요청은 전송하지 않는다.
4. 사용자는 자신의 요청 상태와 완료된 답변을 조회한다. 관리자는 요청과 검사 결과를 조회하며, 관리자 API 응답에는 사용자의 AI 답변이 포함되지 않는다.

## 로컬에서 시작하기

Docker Engine과 Docker Compose를 준비한다. macOS에서는 Docker Desktop을 실행하면 된다. 아래 명령은 저장소 루트(`compose.yaml`이 있는 폴더)에서 실행한다. Compose가 Python 3.12 기반의 Django 컨테이너(`backend`)와 PostgreSQL 17 컨테이너(`db`)를 실행한다.

### 1. 환경변수 준비

처음 실행할 때 저장소 루트에 `.env` 파일을 만든다. 아래는 로컬 개발용 예시다. 두 비밀값은 각자 생성한 값으로 바꾼다. macOS에서는 `openssl rand -hex 32`를 두 번 실행해 서로 다른 값을 만들 수 있다. 기존 환경을 다시 실행할 때는 기존 `.env`를 사용한다.

```dotenv
DJANGO_SECRET_KEY=replace_with_your_local_django_key
POSTGRES_DB=ai_gateway
POSTGRES_USER=local_app
POSTGRES_PASSWORD=replace_with_your_local_db_password
```

위 네 값으로 로컬 서버를 시작하고 DB 연결과 아래 테스트를 확인할 수 있다. 실제 AI 답변을 생성하려면 모델 서비스 연결을 별도로 구성해야 한다. 로컬에서 생성한 `.env`는 Git에서 제외된다.

### 2. 컨테이너 실행 및 DB 초기화

```bash
docker compose up -d --build
docker compose ps
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py check
```

`docker compose ps`에서 `backend`가 실행 중이고 `db`가 `healthy`인지 확인한다. `migrate`는 저장소에 포함된 마이그레이션으로 테이블을 생성한다. 이미 적용했다면 `No migrations to apply.`가 정상이다. `check`의 기대 결과는 `System check identified no issues (0 silenced).`이다.

### 3. Django와 PostgreSQL 연결 확인

```bash
curl -fsS http://127.0.0.1:8000/health/
```

정상 응답은 HTTP 200과 아래 JSON이다. 이 엔드포인트는 Django에서 PostgreSQL에 간단한 쿼리를 실행해 연결을 확인한다.

```json
{"django": "ok", "database": "connected"}
```

응답이 없거나 HTTP 503이면 `docker compose ps`와 `docker compose logs --tail=50 backend db`로 기동 상태를 확인한다.

### 4. 대표 테스트 실행

```bash
docker compose exec backend python manage.py test accounts.test_auth_api gateway.test_intake_api -v 2
```

로그인·로그아웃 및 요청·PDF 접수 테스트를 실행한다. 마지막에 `OK`가 나오면 통과다. Django는 테스트 전용 DB를 생성하고 실행 후 정리한다. 이 테스트는 실제 AI 서비스 연결이나 관리자 계정 사전 등록 없이 실행할 수 있다.

## 코드와 데이터 위치

| 경로 | 내용 |
| --- | --- |
| `backend/accounts/` | 세션 인증, 서비스 권한, 계정 발급·변경 |
| `backend/gateway/` | 요청·PDF 접수, AI 처리 연결, 승인, 이용 이력·통계 |
| `backend/config/` | Django 설정과 URL 구성 |
| `backend/scripts/` | 배포된 API의 동작 확인 스크립트 |
| `docs/` | 요청 접수와 AI 처리 흐름에 관한 개발 문서 |

요청과 처리 결과는 PostgreSQL에 저장하고, PDF 원본은 별도의 비공개 볼륨에 보관한다. 개발을 마칠 때 `docker compose down`으로 컨테이너를 종료하면 다음 실행에도 두 볼륨의 데이터가 유지된다.
