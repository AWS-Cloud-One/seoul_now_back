# 백엔드 검증 작업 공간

중심 담당: 백엔드 검증·AI 추천. 각 기능 담당도 구현과 함께 관련 테스트를 작성한다.

| 위치 | 담당 범위 |
| --- | --- |
| `api/` | HTTP 요청·응답, 상태 코드, 계층 연결, CORS·OpenAPI |
| `unit/core/` | 설정과 공통 기능 |
| `unit/services/` | Service 처리·실패·시간 제한 |
| `unit/ai/` | 조건 해석, 구조화 출력·근거 검증, 모델 실패 대체 동작 |
| `unit/recommendation/` | 고정 후보에 대한 필터·순위·제외 이력 |
| `unit/data_pipeline/` | 정규화·회차 처리의 실패·중복·최신성 규칙 |
| `integration/` | 실제 PostgreSQL/PostGIS와 연결 모듈 간 통합 |
| `fixtures/` | 공유할 작은 원천 표본·모의 응답·재현 자료 |

API용 `app`·`client` fixture는 `api/conftest.py`에 둔다. 여러 테스트 경로에서 쓰는 격리 DB fixture는 루트 `conftest.py`에 두며 명시적으로 요청한 테스트에서만 실행된다. 모든 테스트가 API나 실제 DB를 요구하도록 루트 conftest에서 실행 환경을 강제하지 않는다.

```bash
# DB 없이 실행하는 API·단위 테스트
docker compose run --build --rm --no-deps tests pytest -m "not integration"

# 실제 PostGIS를 포함한 전체 테스트
docker compose --profile test run --build --rm tests

# 코드 규칙·형식 검사
docker compose run --build --rm --no-deps tests ruff check .
docker compose run --build --rm --no-deps tests ruff format --check .

# 테스트 DB만 중지 (서비스 DB에는 영향 없음)
docker compose --profile test stop test-db
```

`integration` 표시는 실제 인프라를 요구하는 테스트에 사용한다. `compose.yaml`의 `test` 프로필은 별도 `test-db`와 전용 볼륨을 사용한다. `tests` 서비스는 이 DB의 `TEST_DATABASE_URL`만 전달받으며 실제 API 키·서비스 원문 파일은 공유하지 않는다. API·수집기 실행은 필요하지 않다. 로컬 Python에서 URL을 지정하지 않으면 DB 통합 테스트는 건너뛴다. PostGIS 연결·공간 거리 조회, Alembic의 모델·DB 비교와 수집 저장 흐름을 검사한다. PostGIS 확장 테이블이 삭제 대상으로 잘못 인식되지 않는지도 확인한다.

`test_migration_0009.py`는 신규 DB·`0007`·수집 `0008`·지도 `0008`·둘 다 적용된 `0008`에서
최종 정형 스키마 0009로 직접 올라가는지 검사한다. 기존 데이터·공간 인덱스·공개 주차 UUID·
0원 요금·시각 의미·원문 숫자 정밀도·부모 관계·제외 사유를 확인한다. 이전 테이블의 모든 셀은
파일 보관본과 비교하며, 파일 쓰기 실패·잘못된 제외 사유에서 스키마·자료·revision이 롤백되는지,
offline SQL 적용과 데이터 손실을 일으킬 downgrade가 차단되는지도 검사한다.
`TEST_DATABASE_URL`의 서버에 테스트마다 무작위 이름의 별도 DB를 생성·삭제하므로
테스트 사용자에게 `CREATEDB` 권한이 필요하다. URL에 지정된 DB 자체는 초기화하지 않는다.

`test_model_constraints.py`는 `model_constraints_<무작위 UUID>` DB를 생성해
현재 `app.models.Base`의 FK·수치·기하·원본 보존 제약을 직접 검증한다.
스키마 제약 검사 외에 수집·API·마이그레이션 통합 검증도 실행한다.

테스트에서 사용하는 고정 입력·기대 결과·모의 응답은 `tests/fixtures/`에 둔다. 실제 모델을 호출하는 품질 평가는 도입 시 실행 명령과 결과 보관 위치를 정한다. 기본 pytest 테스트에서 실제 모델·제공처를 자동 호출하지 않는다.


### 정형 저장·원문 보존

수집/공개 테스트는
`normalized_<UUID>` 임시 DB를 사용한다. `test_publish_parking.py`와 `test_map_postgis.py`도
통합 테스트이며 TEST_DATABASE_URL이 없으면 생략된다.

`unit/test_model_metadata.py`는 현재 모델의 정형 컬럼과 PostgreSQL 테이블·인덱스 DDL 생성을 검증한다.
과거 설계용 import 경로나 작업 기록 파일은 테스트에 사용하지 않는다.

`test_payload_store.py`는 파일 중복 저장·무결성·누락·외부 경로 거부를 검증한다. 파일은 테스트별 임시 디렉터리에만 기록한다.

### 장소·대화 저장 틀

`test_service_foundation.py`는 0009에 포함된 8개 서비스 테이블의 초기 빈 상태, 장소·카테고리 FK, 대화 순서, 같은 여행의 조건 버전 연결, 목적·연령대·제외 장소 중복 방지, 지연 FK와 명시적 세션 정리, downgrade 차단 시 데이터 보존을 검증한다. 추천·채팅 기능 연결 완료를 의미하지 않는다.

### 제품 API와 데이터 검증

`api/test_product_contract.py`는 명세의 제품 경로·모델명·필수 좌표·응답 검증과 삭제한 개발 경로의 미등록을 확인한다. API 테스트는 실제 제품 요청을 대상으로 한다.

수집·원본 보존·공간 조회는 `integration/`에서 실제 테이블과 저장 파일을 검사한다. `integration/conftest.py`의 stored_payload fixture는 출처·대상별 보관 자료를 직접 읽는다. 공식 장소 파일의 121개 코드·닫힌 경계 검증은 `unit/data_pipeline/test_official_files.py`에 있다. 임시 웹 검사 화면이나 폐기한 HTTP 계약을 테스트용 서버로 복제하지 않는다.
