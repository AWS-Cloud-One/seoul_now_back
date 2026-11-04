# 백엔드 검증 작업 공간

`integration/test_spatial_write_changes.py`는 테스트 DB의 `UPDATE OF location/geometry` 트리거로 같은 위치가 SQL 갱신 대상에서 빠지는지 확인한다. 주차·도로·경계·센서의 실제 위치 변경, 식별자 유지, 지도에서 변경된 지물만 반영하는 동작도 검사한다. 운영 스키마에 트리거를 추가하지 않는다.

`integration/test_read_snapshot_isolation.py`는 실제 FastAPI 조회 도중 다른 연결을 커밋해도 지도 버전·위치·관측값이 이전 스냅샷으로 유지되는지 확인한다. 주차 조회의 대기 없는 이전 값 반환·커밋/롤백, 조회 세션의 쓰기 차단, 연결 재사용 후 수집기의 쓰기 허용도 검사한다.

`integration/test_pipeline_publication.py`는 수집→정형 저장→자동 공개→기존 지도 조회를 연결해 검증한다. 정상0·원래 수신 시각·관측 없음·실패/부분 시도·진행 중 시도 제외, 관측 갱신 시 형상/버전 유지, 기준본 불일치 보류, 공개 중 오류의 전체 롤백과 수집 데이터 보존·수동 재시도·중복 방지를 확인한다. 실제 개발 DB에는 쓰지 않는다.

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

현재 `normalized_database`는 새 임시 DB에 `alembic upgrade head`만 실행한다. 정식 head는 0010이며 모델34개와 일치한다. 별도 초안 호출 없이 실제 배포와 같은 경로를 검증하고 개발 DB에는 접근하지 않는다.

`integration/test_sensor_storage.py`는 설치만 존재하는 센서·정상 0·이전 유효 관측과 최근 실패/충돌의 분리, 회차/관측/수신 시각 역행, DATA_NO 변경, 기준본 교체·비활성화 제한, 장비 변경 시 관측 근거 분리, 동시 저장·롤백, XLSX/페이지 원문 대응을 검사한다. 설치 fixture는 실제 XLSX 바이트를 생성하며 보관 파일을 다시 읽어 무결성을 검증한다. 테스트 경계는 합성 자료로, 실제 서울 설치 개수나 전체 센서 최신성을 검증하지 않는다.

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
0009를 거쳐 현재 head 0011까지 올라가는지 검사한다. 기존 데이터·공간 인덱스·공개 주차 UUID·
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

`unit/data_pipeline/test_citydata_sections.py`는 선택 전후 소비 필드 보존과 섹션 구분을 확인한다. `integration/test_citydata_section_storage.py`는 인구 SQL 실패 시 다른 섹션 보존, 오류 봉투 저장/미사용, 이전 자료 재사용·수신 시각 유지, 정상 빈 목록, 선택본 소비, 시영주차 보완본의 부모 연결을 격리 DB에서 검사한다. 회차의 섹션 집계와 주차 투영의 실제 행 집계를 구분한다.

`test_migration_0010.py`는 기존 0009의 운영시간 문자열·공개 UUID·0원 요금·도로 매핑 키/미확인 순번을 보존하는지 검사한다. 마지막 구형 컬럼 제거 직전 오류를 주입해 신규 테이블·컬럼·이관 데이터·revision이 함께 롤백되는지 확인하고, 이후 정상 재시도·head 재실행·모델 일치를 검사한다. 0010은 고정 DDL을 사용하며 런타임 모델/전처리 코드를 import하지 않는다.
