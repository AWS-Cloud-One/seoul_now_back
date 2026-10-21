# 서울 지금 어디 — 백엔드

사용자의 조건에 맞는 서울의 장소를 추천하고, 지도에 필요한 정보를 제공하는 백엔드입니다. **백엔드, 백엔드 검증·AI 추천, 데이터 처리, AWS 담당이 함께 사용하는 저장소**입니다.

현재 FastAPI·PostGIS 기반에 공영주차장과 서울시 OA-21285 통합 도시데이터 수집이 구현되어 있습니다. 유동인구·도로·대중교통·주차·상권 등 전체 응답을 장소별로 저장합니다. 실행은 [초래님 실행 안내](DATA_PIPELINE_GUIDE.md)를 참고하세요.

현재 작업 범위: AI 추천·제품 프론트엔드·AWS 배포 자동화는 해당 담당의 후속 작업입니다. 데이터 담당은 공영주차장과 OA-21285 통합 도시데이터를 별도 수집 명령으로 적재합니다.

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
| 백엔드 검증·AI 추천 | 조건 해석·설명, 후보 비교·순위, API·AI 품질 검증 | [AI](app/modules/ai/README.md), [추천](app/modules/recommendation/README.md), [테스트](tests/README.md), [평가](evals/README.md) |
| 데이터 처리 | 교통·인구·주차 수집, 정규화·적재, 최신성·실패 상태 관리 | [데이터 처리](app/modules/data_pipeline/README.md), [수집 실행](jobs/README.md), [로컬 자료](data/README.md) |
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
├── evals/               # AI·추천 평가 사례와 결과
├── data/                # 로컬 원본·중간 결과
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

Service가 AI·추천 모듈을 호출하고 결과를 조립합니다. **수집 작업은 `jobs/`에서 별도 실행**하며, API와 공통 모델·Repository·DB를 사용합니다. API 서버를 켠다고 수집이 자동으로 시작되지는 않습니다.

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

</details>

## 테스트 실행하기

먼저 `docker compose up --build -d`로 개발 이미지를 준비합니다. 아래 명령은 컨테이너 안에서 실행하므로 로컬 Python이 필요하지 않습니다.

```bash
# 코드 규칙·형식 검사
docker compose run --rm --no-deps api ruff check .
docker compose run --rm --no-deps api ruff format --check .

# 실제 DB 없이 API·단위 테스트만 실행
docker compose run --rm --no-deps api pytest -m "not integration"

# 실제 PostgreSQL/PostGIS를 포함한 전체 테스트
docker compose --profile test run --build --rm tests
```

기본 테스트에서는 외부 API나 유료 모델을 호출하지 않습니다. `tests` 서비스는 테스트용 프로필에서만 실행되며, DB 준비·migration 적용 후 공간 쿼리를 포함한 검사를 진행합니다. `--build`로 실행해야 패키지 변경 후에도 최신 개발 의존성을 검사합니다. 자세한 구분과 작성 위치는 [테스트 안내](tests/README.md)를 참고하세요.

<details>
<summary>DB 변경 상태와 SQL 확인하기</summary>

```bash
docker compose exec api alembic current
docker compose exec api alembic check
docker compose run --rm --no-deps api alembic upgrade head --sql
```

현재 적용된 revision, 모델과 DB의 차이, migration의 SQL을 확인합니다. 모델 변경 후 자동 생성한 migration은 내용과 적용 순서를 검토하세요.

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

## 현재 구현·검증 상태

2026-10-19, Docker Desktop에서 다음을 확인했습니다.

- 개발 이미지와 arm64·amd64 배포 이미지 빌드, 고정 패키지의 `pip check` 통과. amd64에서도 앱 import·비동기 PostGIS 연결 확인.
- Compose의 DB 준비 → migration 성공 → API 시작, API·DB의 healthy 상태 확인.
- API·단위·PostGIS 통합 테스트 **17개 통과**, Ruff 규칙·형식 검사 통과.
- 호스트에서 `/live`, `/ready`, `/docs`, `/openapi.json`의 HTTP 200 확인.
- 초기 revision `0001` 적용, Alembic 모델 비교와 SQL 생성 확인. PostGIS 보조 테이블을 삭제 대상으로 잡지 않도록 회귀 검사 추가.
- 배포용 이미지의 일반 사용자 실행, 개발 도구·개인 파일 제외, 실제 DB 연결 확인.

테스트는 통과하지만 Starlette TestClient가 기존 HTTPX 사용에 대한 **사용 중단 예정 경고 1건**을 출력합니다. 현재 조합의 테스트는 정상 동작하며 경고를 숨기지는 않았습니다. 테스트 클라이언트 의존성을 변경할 때 별도로 검토합니다.

2026-10-22 현재 OA-21285 전체 도시데이터를 실제 호출해 광화문·덕수궁(POI009) 스냅샷을 저장했고, API에서 유동인구 4,000~4,500명과 응답 15개 섹션을 확인했습니다. 수집은 장소별 단발 작업이며 자동 갱신은 연결하지 않았습니다. 상세 범위와 실행은 DATA_PIPELINE_GUIDE.md 및 app/modules/data_pipeline/IMPLEMENTATION_STATUS.md를 참고하세요. AI 추천, 제품 프론트엔드, AWS 자동 배포는 별도 담당 작업입니다.

`.env`, 가상환경, 로컬 원본·중간 데이터와 평가 생성 결과는 Git에서 제외됩니다. `agent/`와 `AGENTS.md`도 개인 작업용으로 제외하며, 팀원이 필요한 공통 안내는 이 README와 각 디렉터리의 README에서 관리합니다. 개인 문서와 로컬 데이터는 Docker 빌드에서도 제외합니다.
