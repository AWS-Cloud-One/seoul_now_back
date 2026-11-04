# 백엔드 검증

API·단위 테스트는 외부 API를 호출하지 않는다. DB 통합 테스트는 `TEST_DATABASE_URL` 서버에 테스트별 임시 DB를 생성하고 종료 시 삭제한다. 개발 DB 자체를 초기화하지 않는다. 테스트 계정은 `CREATEDB` 권한이 필요하며 URL을 지정하지 않으면 DB 통합 테스트는 생략한다.

| 위치 | 검증 범위 |
| --- | --- |
| `api/` | 제품 HTTP 계약·응답 검증·계층 연결 |
| `unit/` | 설정·서비스·정규화·수집 제어·스키마 메타데이터 |
| `integration/` | 실제 PostgreSQL/PostGIS·마이그레이션·트랜잭션·저장/조회 연결 |
| `fixtures/` | 작은 합성 입력·고정 기대값·모의 응답 |

API fixture는 `api/conftest.py`, 격리 DB fixture는 루트 `conftest.py`에 둔다. `normalized_database`는 새 DB에 일반 `alembic upgrade head`를 실행한다. 최신 revision은 `0012`이며 앱 모델 34개와 비교한다. 별도 초안 적용 설정은 없다.

```bash
# API·단위 테스트
docker compose run --build --rm --no-deps tests pytest -m "not integration"

# 별도 PostGIS를 포함한 전체 테스트
docker compose --profile test run --build --rm tests

# 코드 규칙·형식 검사
docker compose run --build --rm --no-deps tests ruff check .
docker compose run --build --rm --no-deps tests ruff format --check .

# 테스트 DB만 중지
docker compose --profile test stop test-db
```

Compose의 `test-db`는 서비스 DB와 다른 볼륨을 사용하며 호스트 포트를 열지 않는다. `tests`에는 실제 API 키를 전달하지 않는다. 공식 파일 파서 검사용 현재 기준파일만 읽기 전용으로 연결한다.

## 직접 적재와 일관성

- `test_direct_reference_storage.py`: 장소 목록·경계 불일치 시 기존값 보존, 도로선·매핑 원자적 교체, 센서 설치/관측 분리, 실패 후 정상값 유지, `0011 → 0012` 이관·DDL 오류 시 전체 롤백·정상 재시도.
- `test_citydata_pipeline.py`·`test_citydata_section_storage.py`: 현재 응답의 인구·주차 직접 적재, 한 분야 실패 시 다른 성공분 저장, 빈 목록·오류 봉투·과거 시각 처리.
- 주차·전국 주차 테스트: 출처별 요금/운영시간/현황, 충돌 보류, 실패 시 정상값 보존, 데이터와 페이지 진행의 동시 커밋, 재시작·오래된 배치 거절.
- `test_topis_pipeline.py`·`test_topis_retention.py`: 도로별 재시도·배치/타이머 커밋·취소·중단 복구와 커밋된 성공분 보존.
- `test_collection_summaries.py`: 단일 회차의 실제 집계, 출처별 종료된 최신 20회 유지, 로그 삭제 후 관측·최근 시도·페이지 진행 보존.
- `test_spatial_write_changes.py`·`test_direct_reference_storage.py`·`test_sgis_geometry_storage.py`: 같은 위치/도형의 공간 UPDATE 생략, 실제 변경 반영과 관계 갱신.
- `test_read_snapshot_isolation.py`: 갱신 중 커밋 전 값 조회, HTTP 응답 안의 버전·위치·관측 일관성, 읽기 세션 쓰기 차단.
- `test_pipeline_publication.py`: 수집·정형 저장·지도 공개·기존 API 연결, 공개 실패 롤백과 DB 재공개.
- `test_scheduler_lease.py`·`test_pipeline_recovery.py`: 중복 실행 방지, DB 선행조건, 파일 없는 중단 회차 정리·공개 복구.

`no_runtime_files` fixture는 수집·지도 조회·복구 중 data 경로 접근을 차단한다. 원문 저장소와 모델이 제거됐음을 확인하며, 삭제한 파일 보관 기능을 테스트 때문에 런타임에 유지하지 않는다.

## 스키마와 기존 기능

`test_migration_0009.py`는 신규·0007·충돌한 0008 이력에서 0009를 거쳐 최신 head까지 전환한다. 0009 당시 보관 동작은 테스트 전용 임시 경로에서만 검증한다. 현재 코드의 파일 저장소를 다시 사용하지 않는다. 최신까지 적용하는 일반 경로에서는 과거 이관 중간 파일을 임시 디렉터리와 함께 삭제한다.

`test_migration_0010.py`·`test_migration_0011.py`는 기존 운영시간·UUID·요금·관측/시도 시각을 보존하고 오류 시 스키마·데이터·revision이 함께 롤백되는지 검사한다. 과거 revision은 고정 DDL을 사용한다. `test_model_constraints.py`는 현재 FK·수치·기하 제약을 확인한다.

`test_service_foundation.py`는 장소·대화 등 기존 저장 틀과 FK/순서 제약을 검증한다. 추천·대화 기능 구현 완료를 의미하지 않는다. `api/test_product_contract.py`는 제품 경로·필드명·필수 좌표와 폐기한 개발 경로의 미등록을 확인한다.
