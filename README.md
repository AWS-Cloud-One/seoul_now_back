# 서울 지금 어디 — 백엔드

사용자의 조건에 맞는 서울의 장소를 추천하고, 지도에 필요한 정보를 제공하는 백엔드입니다. **백엔드, 백엔드 검증·AI 추천, 데이터 처리, AWS 담당이 함께 사용하는 저장소**입니다.

주기적 API 응답에서 필요한 필드를 추출·검증해 정형 DB에 직접 저장합니다. 응답 원문·가공본·처리 보고서를 보관하거나 재생하지 않습니다. 실패한 대상의 이전 정상값은 유지하고 최근 시도 상태와 짧은 회차 요약만 기록합니다.

현재 기준파일은 `data/`에 유지합니다. 운영자가 새 파일로 교체한 뒤 명시적인 반영 작업을 실행하면 검증된 위치·경계·매핑을 DB에 저장합니다. API·정기 수집·지도 공개는 DB의 기준정보만 읽습니다. 최신 스키마는 `0012`입니다.

주차·도로·인구 수집/가져오기는 정형 저장 후 기존 지도 공개본 갱신까지 연결됩니다. 실시간 수집은 보통 관측만 갱신하며, 검증된 정형 기준본과 공개본이 달라졌을 때만 기준본도 함께 복구합니다. 스케줄러는 수집 시작 전과 비정상 종료 후에 커밋된 자료의 공개를 재시도합니다. 공개 결과는 `publication`으로 구분하며 수동 `jobs.publish_map` 명령도 사용할 수 있습니다. [실행별 공개 범위](jobs/README.md#저장-후-지도-공개)를 확인하세요.

> 처음 참여했다면 **역할별 작업 위치 → 처음 실행하기 → 서버 확인하기** 순서로 읽어 주세요. 필요한 세부 설명은 각 디렉터리의 README에 있습니다.

- [역할별 작업 위치](#역할별-작업-위치)
- [프로젝트 구조와 요청 흐름](#프로젝트-구조와-요청-흐름)
- [처음 실행하기](#처음-실행하기)
- [서버 확인하기](#서버-확인하기)
- [다음 날 다시 개발할 때](#다음-날-다시-개발할-때)
- [패키지와 Python 버전 관리](#패키지와-python-버전-관리)
- [새 기능을 추가할 때](#새-기능을-추가할-때)
- [테스트 실행하기](#테스트-실행하기)
- [설정과 자주 겪는 문제](#설정과-자주-겪는-문제)
- [현재 구현·검증 상태](#현재-구현검증-상태)

## 역할별 작업 위치

| 담당 | 하는 일 | 먼저 볼 위치 |
| --- | --- | --- |
| 백엔드 | 화면의 요청을 받고, 데이터·AI·추천 결과를 응답으로 연결 | `app/api/`, `app/services/`, `app/schemas/` |
| 백엔드 검증·AI 추천 | 조건 해석·설명, 후보 비교·순위, API·AI 품질 검증 | [AI](app/modules/ai/README.md), [추천](app/modules/recommendation/README.md), [테스트](tests/README.md) |
| 데이터 처리 | 교통·인구·주차 수집, 정규화·적재, 최신성·실패 상태 관리 | [데이터 처리](app/modules/data_pipeline/README.md), [수집 실행](jobs/README.md), [입력 자료](jobs/README.md#공식-입력-자료) |
| AWS | 실행 이미지, 인프라, 배포 자동화와 운영 환경 구성 | [인프라](infra/README.md), [이미지](infra/docker.md), [워크플로](.github/workflows/README.md) |

**DB 모델·조회 코드·마이그레이션·외부 HTTP 연결은 백엔드와 데이터 처리 담당이 공동으로 관리합니다.** 담당별로 별도의 DB 연결이나 같은 제공처 클라이언트를 중복 작성하지 않습니다.

## 프로젝트 구조와 요청 흐름

```text
seoul_now_back/
├── app/
│   ├── main.py          # FastAPI 앱 생성과 시작·종료 처리
│   ├── api/             # 요청을 받는 경로와 객체 연결
│   ├── core/            # 공통 설정과 예외
│   ├── db/              # DB 엔진과 공통 모델 기반
│   ├── models/          # DB 테이블을 표현하는 Python 클래스
│   ├── schemas/         # 요청·응답 데이터의 형식
│   ├── services/        # 한 요청을 처리하는 순서와 업무 규칙
│   ├── repositories/    # DB 조회·저장 코드
│   ├── clients/         # 장소·경로·공공 API 통신
│   └── modules/         # AI·추천·데이터 처리 기능
│       ├── ai/              # 자연어 조건 해석·추천 설명
│       ├── recommendation/  # 서버의 후보 선정·필터·비교
│       └── data_pipeline/   # 수집·정규화·회차별 적재
├── jobs/                # API와 별도로 실행할 단발 수집 작업
├── migrations/          # DB 구조의 변경 이력
├── tests/               # API·단위·통합 테스트와 표본
├── data/                # 명시적 기준정보 반영에 쓰는 현재 기준파일
├── infra/               # AWS 인프라·환경 설정·운영 절차
├── .github/workflows/   # GitHub Actions 작성 위치
├── .env.example         # 로컬 설정 예제
├── Dockerfile           # Python 버전·패키지 설치·이미지 실행 설정
├── compose.yaml         # DB → migration → API 실행 순서
├── requirements.txt     # 직접 사용하는 실행 패키지와 버전
├── requirements-dev.txt # 실행 패키지 참조 + 테스트·검사 도구
└── pyproject.toml       # Ruff·pytest 설정
```

**일반적인 API 개발은 `app/`의 기본 계층에서, AI·추천·데이터 처리 개발은 `app/modules/`에서 시작합니다.** DB 조회·저장과 외부 API 통신은 여러 기능에서 공유하므로 `repositories/`와 `clients/`를 공통 위치에 둡니다.

API의 기본 처리 흐름은 다음과 같습니다.

```text
프론트 요청 → Router → Service → Repository → DB
                          ├→ modules/의 AI·추천 기능
                          └→ clients/의 외부 API 통신
```

| 용어 | 쉽게 말하면 |
| --- | --- |
| Router | 어떤 URL의 요청을 어느 기능으로 보낼지 정하는 입구 |
| Schema | 요청·응답에 어떤 필드와 값이 필요한지 정한 형식 |
| Service | 필요한 작업을 순서대로 연결하고 업무 규칙을 적용하는 코드 |
| Repository | DB에서 데이터를 읽고 저장하는 코드 |
| Model | DB 테이블·컬럼을 Python으로 표현한 클래스 |
| Migration | DB 구조가 바뀔 때 순서대로 적용하는 변경 기록 |

Service가 AI·추천 모듈을 호출하고 결과를 조립합니다. **수집 작업은 `jobs/`의 별도 worker에서 실행**하며 API와 공통 모델·Repository·DB를 사용합니다. `docker compose up`은 마이그레이션 이후 수집 worker도 실행합니다. API 프로세스만 실행하면 자동 수집은 시작되지 않습니다.

`app/api/`는 제품 HTTP 요청·응답과 명세의 헬스체크를 담당합니다. 임시 데이터 검사 화면·원본 조회·구형 API 호환 경로는 제공하지 않습니다. 수집·저장·공간 정보의 검증은 `tests/unit/data_pipeline/`과 `tests/integration/`에서 수행합니다. `/parking/nearby`는 명세의 `latitude`·`longitude`를 필수로 받습니다.

Service·Repository·외부 클라이언트는 `app/api/dependencies.py`에서 연결합니다. Service는 `app/core/exceptions.py`의 `ServiceError(code, message, ...)`를 전달하고, HTTP 상태 코드는 `app/api/errors.py`, 제품 응답 봉투는 `app/api/contract.py`에서 처리합니다. 추천 담당의 검증기·엔진은 `get_recommendation_service()`에 연결하며 실제 알고리즘은 담당 모듈에서 구현합니다.

채팅·추천 경로 재확인·장소 상세·이동시간 확인·수집 상태 조회는 `pending` 라우터·스키마·서비스에 연결 틀만 준비되어 있습니다. 어댑터 구성은 `get_pending_service()`에서 담당하며 미연결 기능은 503을 반환합니다. 수집 상태 조회는 관리자 인증 연결 전까지 차단됩니다. 대화 서비스는 처리 결과와 `ServiceError`를 반환하고 HTTP 상태·응답 봉투는 라우터가 구성합니다.

현재 공통 API 조회 세션은 요청별 읽기 전용 스냅샷입니다. 대화·조건 등 DB에 저장하는 엔드포인트를 구현할 때는 쓰기 세션과 커밋 경계를 명시적으로 연결해야 합니다. 수집기는 공통 기본 엔진의 쓰기 세션을 사용합니다.

## 처음 실행하기

**Docker Desktop과 Git만 준비하면 됩니다.** 로컬에 Python·pip·uv를 따로 설치하지 않아도 됩니다. 첫 빌드에는 이미지와 패키지를 받을 인터넷 연결이 필요합니다.

### 1. 저장소 폴더 열기

이미 프로젝트를 받았다면 `Dockerfile`과 `compose.yaml`이 있는 백엔드 폴더로 이동하세요. 아직 받지 않았다면 프로젝트를 둘 상위 폴더에서 다음 명령을 실행합니다.

```bash
git clone https://github.com/AWS-Cloud-One/seoul_now_back.git
cd seoul_now_back
```

이후 명령은 모두 이 폴더에서 실행합니다. Docker Desktop을 켜고 아래 명령이 실행되는지 확인하세요.

```bash
docker version
docker compose version
```

### 2. API와 DB 함께 실행하기

```bash
docker compose up --build -d
```

이 명령 하나로 다음 작업을 진행합니다.

1. Dockerfile에 지정된 Python 이미지로 백엔드 이미지를 만듭니다.
2. `requirements.txt`와 `requirements-dev.txt`의 패키지를 이미지에 설치합니다.
3. PostgreSQL/PostGIS가 준비될 때까지 기다립니다.
4. `migrate` 컨테이너에서 `alembic upgrade head`를 한 번 실행합니다.
5. migration이 성공하면 API 개발 서버를 시작합니다.

처음에는 다운로드·설치에 시간이 걸릴 수 있습니다. 패키지는 **이미지 빌드 때** 설치하며, 컨테이너를 시작할 때마다 다시 설치하지 않습니다. 코드는 로컬 폴더와 연결되어 수정하면 개발 서버가 자동으로 다시 읽습니다.

기본 설정으로 바로 실행되므로 `.env`는 필수가 아닙니다. 포트나 프론트 주소를 바꾸려면 `.env.example`을 `.env`로 복사한 뒤 수정하세요. `.env`는 Git과 이미지 빌드 대상에서 제외됩니다.

### 3. 상태 확인하기

```bash
docker compose ps -a
docker compose logs --tail=50 api migrate
```

정상 실행 시 `db`와 `api`는 `healthy`, `migrate`는 `Exited (0)` 상태가 됩니다. **migrate의 정상 종료는 오류가 아닙니다.** DB 변경을 적용하고 종료하는 일회성 작업입니다.

## 서버 확인하기

브라우저에서 아래 주소를 열어 확인합니다.

| 주소 | 확인할 내용 |
| --- | --- |
| [API 문서](http://127.0.0.1:8000/docs) | API와 입력·응답 형식. `Try it out`으로 요청을 시험할 수 있습니다. |
| [서버 생존 확인](http://127.0.0.1:8000/api/v1/health/live) | API가 실행 중이면 `{"status":"ok"}`와 HTTP 200을 반환합니다. |
| [DB 준비 상태 확인](http://127.0.0.1:8000/api/v1/health/ready) | DB·PostGIS를 사용할 수 있으면 200, 사용할 수 없으면 503을 반환합니다. |
| [OpenAPI 명세](http://127.0.0.1:8000/openapi.json) | 프론트와 공유할 API 형식을 JSON으로 확인합니다. |

처음 코드를 읽는다면 헬스체크를 따라가 보세요: [Router](app/api/routes/health.py) → [Service](app/services/health.py) → [Repository](app/repositories/health.py). 객체를 연결하는 곳은 [dependencies.py](app/api/dependencies.py)입니다.

## 다음 날 다시 개발할 때

Docker Desktop을 실행하고 백엔드 폴더에서 다음 명령을 사용합니다.

```bash
docker compose up -d
```

패키지나 Dockerfile이 바뀌었거나 팀원의 환경 변경을 받았다면 다시 빌드합니다.

```bash
docker compose up --build -d
```

서버 로그를 계속 보려면 `docker compose logs -f api`를 사용합니다. 로그 화면에서 `Ctrl+C`를 눌러도 백그라운드 서버는 유지됩니다.

작업을 마칠 때는 다음 명령으로 API와 DB를 종료합니다. DB 데이터는 named volume에 보존됩니다.

```bash
docker compose down
```

## 패키지와 Python 버전 관리

| 파일 | 역할 | 수정하는 시점 |
| --- | --- | --- |
| [Dockerfile](Dockerfile) | Python 3.14.7과 OS 이미지, 설치·실행 방식 | Python·OS 버전을 바꿀 때 |
| [requirements.txt](requirements.txt) | 직접 사용하는 실행 패키지와 버전 | 기능에 필요한 패키지를 추가할 때 |
| [requirements-dev.txt](requirements-dev.txt) | 실행 목록 참조와 pytest·Ruff 같은 개발 도구 | 개발 도구를 추가할 때 |
| [pyproject.toml](pyproject.toml) | Ruff·pytest의 검사 설정 | 코드 규칙·테스트 설정을 바꿀 때 |

Python은 Dockerfile에서 관리합니다. 패키지는 requirements 두 파일을 직접 수정하며, `패키지명==버전`으로 사용할 버전을 지정합니다. 패키지가 내부적으로 사용하는 하위 의존성은 pip가 호환 조건에 따라 설치합니다.

`requirements-dev.txt`의 `-r requirements.txt`는 실행 패키지 목록을 함께 읽으라는 뜻입니다. 실행 패키지를 두 파일에 중복 작성하지 않습니다. 배포용 이미지는 실행 패키지만, 개발용 이미지는 개발 도구까지 설치합니다.

패키지를 추가하거나 버전을 바꿨다면 해당 파일을 저장한 뒤 다시 빌드합니다.

```bash
# 변경된 패키지로 API 이미지 다시 빌드·실행
docker compose up --build -d

# 테스트 실행
docker compose --profile test run --build --rm tests
```

Python·PostGIS 이미지는 태그와 digest를 함께 고정했으므로 업데이트할 때는 두 값을 함께 검토합니다. 자세한 빌드 단계와 배포용 이미지는 [Docker 안내](infra/docker.md)를 참고하세요.

## 새 기능을 추가할 때

API 담당자는 다음 순서로 파일을 추가하면 됩니다. 같은 기능은 `parking.py`처럼 이름을 맞추면 찾기 쉽습니다.

1. **Schema:** `app/schemas/`에 요청·응답 형식을 정합니다.
2. **Model·Migration:** 테이블이 필요하면 `app/models/`에 작성하고 `app/models/__init__.py`에 등록합니다. 모델은 `app/db/base.py`의 공통 `Base`를 상속합니다.
3. **Repository:** `app/repositories/`에 DB 조회·저장 메서드를 작성합니다.
4. **Service:** `app/services/`에서 필요한 데이터·AI·추천 처리를 연결합니다.
5. **Router:** `app/api/routes/`에 API를 작성하고 `app/api/router.py`에 등록합니다. 객체 생성·연결은 `app/api/dependencies.py`에 작성합니다.
6. **Test:** `tests/api/`, `tests/unit/`, `tests/integration/` 중 해당 범위에 테스트를 추가합니다.

AI·추천·데이터 담당자는 [역할별 작업 위치](#역할별-작업-위치)의 모듈 안내에 따라 구현하고, 공통 Schema와 Repository를 API 담당자와 맞춥니다. 쓰기 트랜잭션은 API의 Service 또는 수집 pipeline에서 관리합니다. Repository가 개별적으로 커밋하지 않도록 합니다.

<details>
<summary>DB 테이블을 변경할 때 사용하는 명령</summary>

모델 작성·등록 후 변경 파일을 생성합니다. 자동 생성 결과를 검토한 다음 실제 DB에 적용합니다.

```bash
docker compose exec api alembic revision --autogenerate -m "describe schema change"
# 생성된 migration의 upgrade/downgrade 내용을 확인한 뒤 실행
docker compose run --rm migrate
```

마이그레이션은 담당별로 나누지 않고 하나의 `migrations/`에서 관리합니다. 현재 앱 테이블은 기본 `public` 스키마에 작성하며, Alembic도 이 스키마를 기준으로 비교합니다. API 프로세스가 직접 실행하지 않고 별도 `migrate` 컨테이너로 적용합니다. 최초 revision을 되돌려도 다른 테이블이 사용할 수 있는 PostGIS 확장은 삭제하지 않습니다.

현재 최신 revision은 **`0012`**, 앱 모델은 **34개 테이블**입니다. 기존0008 통합과0010·0011을 거쳐0012에서 원문 테이블/FK를 제거하고 관측 수신 시각·최근 시도·전국 주차 진행을 정형 구조로 분리합니다. DDL과 이관 로직은 revision 안에 고정하며 현재 앱 모델로 과거 migration을 다시 생성하지 않습니다.

API·수집 worker를 중지한 상태에서 적용합니다. `stamp`, 볼륨 삭제, DB 초기화로 우회하지 않습니다.

```bash
docker compose run --build --rm migrate
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic check
```

정상 결과는 `0012 (head)`와 `No new upgrade operations detected.`입니다.0012는 기존 정형값과 실제 수신/시도 시각을 보존하며 원문 내용은 제거하므로 downgrade로 복원할 수 없습니다.

API·수집·지도 조회에는 원문 디렉터리나 `APP_PAYLOAD_STORAGE_DIR`가 필요하지 않습니다. 빈 DB에서 최신까지 올릴 때 과거0009의 중간 파일은 임시 디렉터리에서 처리한 뒤 삭제합니다. 과거 revision 자체를 검증하는 테스트만 명시적인 일시 경로를 사용합니다.

</details>

## 테스트 실행하기

아래 명령은 필요한 이미지를 빌드해 컨테이너 안에서 실행하므로 로컬 Python이 필요하지 않습니다. API·수집기를 먼저 실행할 필요는 없습니다.

```bash
# 코드 규칙·형식 검사
docker compose run --build --rm --no-deps tests ruff check .
docker compose run --build --rm --no-deps tests ruff format --check .

# 실제 DB 없이 API·단위 테스트만 실행
docker compose run --build --rm --no-deps tests pytest -m "not integration"

# 실제 PostgreSQL/PostGIS를 포함한 전체 테스트
docker compose --profile test run --build --rm tests
```

테스트는 외부 API나 유료 모델을 호출하지 않습니다. `compose.yaml`의 `test` 프로필에 있는 `test-db`는 서비스 DB와 별도 볼륨을 사용하며 호스트에 포트를 열지 않습니다. `tests`에는 실제 API 키·원문 파일을 연결하지 않습니다. 각 DB 쓰기 테스트가 임시 DB를 생성해 마이그레이션을 적용하고 검사 후 삭제합니다. `--build`로 최신 코드와 의존성을 검사하며, 테스트 후에는 `docker compose --profile test stop test-db`로 테스트 DB만 중지할 수 있습니다. 자세한 구분과 작성 위치는 [테스트 안내](tests/README.md)를 참고하세요.

<details>
<summary>DB 변경 이력과 적용 상태 확인하기</summary>

```bash
docker compose exec api alembic current
docker compose exec api alembic check
docker compose run --rm --no-deps api alembic history
```

현재 적용된 revision, 모델과 DB의 차이, migration 순서를 확인합니다. 0009의 테이블 생성 SQL은 해당 Python 파일의 `SCHEMA_SQL`에 있으며 별도 SQL·frozen 디렉터리는 사용하지 않습니다. 과거 데이터 이관을 실행하는 0009는 `--sql`만으로 적용할 수 없습니다. 모델 변경 후 자동 생성한 migration은 내용과 적용 순서를 검토하세요.

</details>

## 설정과 자주 겪는 문제

[.env.example](.env.example)을 `.env`로 복사하면 설정을 바꿀 수 있습니다. Compose가 필요한 값을 컨테이너 환경변수로 전달합니다.

| 설정 | 의미 |
| --- | --- |
| `API_PORT` | 내 컴퓨터에서 API에 접속할 포트. 기본 `8000`. |
| `DB_PORT` | 내 컴퓨터의 DB 도구에서 접속할 포트. 기본 `5432`. |
| `APP_DATABASE_URL` | API·migration·테스트가 사용할 DB 주소. Compose 안에서는 호스트가 **`db`**여야 합니다. |
| `APP_CORS_ORIGINS` | 허용할 프론트 주소의 JSON 배열. 기본 localhost/127.0.0.1의 `5173` 포트. |
| `APP_DOCS_ENABLED` | `false`이면 API 문서와 OpenAPI 경로를 끕니다. |
| `APP_DB_CONNECT_TIMEOUT_SECONDS` | DB 연결·연결 풀 대기 제한. 기본 3초. |
| `APP_HEALTH_TIMEOUT_SECONDS` | DB 준비 상태 조회 제한. 기본 5초. |

기본 DB 계정은 사용자 `seoul`, 비밀번호 `seoul_local`, DB 이름 `seoul_now`이며 로컬 개발 전용입니다. 컨테이너에서는 `db:5432`, 내 컴퓨터의 DB 도구에서는 `127.0.0.1:5432`로 연결합니다. `DB_PORT`를 바꿔도 컨테이너 사이의 포트는 `5432`로 유지합니다.

| 증상 | 확인할 내용 |
| --- | --- |
| Docker daemon에 연결할 수 없음 | Docker Desktop이 실행 중인지 확인합니다. |
| 이미지·패키지 다운로드 실패 | 인터넷·Docker 레지스트리·PyPI 접근 상태를 확인한 뒤 다시 빌드합니다. |
| migrate가 0 이외의 코드로 종료 | `docker compose logs migrate`에서 DB 설정·migration 오류를 확인합니다. 성공해야 API가 시작됩니다. |
| `/ready`가 503 | `docker compose logs api db`와 DB 상태를 확인합니다. 이전 로컬 설정의 `APP_DATABASE_URL`이 `127.0.0.1`이면 `db`로 수정합니다. |
| 8000 또는 5432 포트 사용 중 | `.env`의 `API_PORT` 또는 `DB_PORT`를 바꾸고 `docker compose up -d`를 다시 실행합니다. |
| 추가한 패키지를 찾을 수 없음 | `requirements.txt` 또는 `requirements-dev.txt` 수정 → `docker compose up --build -d` 순서를 확인합니다. |
| 새 모델이 migration에 나타나지 않음 | `app/models/__init__.py`에 모델을 import하고 `__all__`에도 등록했는지 확인합니다. |

기본 포트는 내 컴퓨터의 `127.0.0.1`에만 연결됩니다. PostGIS 이미지는 `linux/amd64`로 실행하므로 Apple Silicon에서는 Docker Desktop의 amd64 에뮬레이션이 필요합니다. Python API 이미지는 호스트에 맞는 아키텍처로 빌드됩니다.

## 현재 데이터 처리 구조

- 주차·도로·인구·통합 도시데이터는 필요한 필드를 검증하고 정형 저장합니다. 일부 실패 시 정상 처리분과 실패 대상의 마지막 정상값을 유지합니다.
- 같은 위치·도형은 다시 쓰지 않으며, API는 요청별 읽기 전용 스냅샷으로 커밋된 값만 조회합니다.
- 수집 요약은 실행당1개, 출처별 종료된 최신20회만 유지합니다. 관측·최근 시도·전국 페이지 진행은 로그 삭제와 독립적입니다.
- 기준자료는 파일 교체 후 별도 검증·반영 작업으로 갱신합니다. 초기 준비는 마이그레이션, 선행 경계 수집, 기준정보 반영, 최초 관측 수집 순서입니다. [수집·반영 명령](jobs/README.md)을 참고하세요.
- 실제 제공처의 결측·시간 지연·호출 한도는 성공 여부와 별개로 기록합니다. 엔드포인트·추천·AI 기능 구현은 각 담당 범위입니다.

`.env`, 가상환경, 개인 작업 문서와 로컬 자료는 Git·실행 이미지에서 제외합니다. 기준파일은 Git으로 공유하되 명시적 반영 작업과 파서 테스트에만 연결합니다.
