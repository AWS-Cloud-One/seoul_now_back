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

API용 `app`·`client` fixture는 `api/conftest.py`에 둔다. DB 통합 fixture가 필요하면 `integration/conftest.py`에 추가한다. 모든 테스트가 API나 실제 DB를 요구하도록 루트 conftest에서 실행 환경을 강제하지 않는다.

```bash
# 저장소 루트에서 개발 이미지와 DB 준비
docker compose up --build -d

# DB 없이 실행하는 API·단위 테스트
docker compose run --rm --no-deps api pytest -m "not integration"

# 실제 PostGIS를 포함한 전체 테스트
docker compose --profile test run --build --rm tests

# 코드 규칙·형식 검사
docker compose run --rm --no-deps api ruff check .
docker compose run --rm --no-deps api ruff format --check .
```

`integration` 표시는 실제 인프라를 요구하는 테스트에 사용한다. `tests` 서비스가 `TEST_DATABASE_URL`을 전달하며, 값이 없으면 현재 DB 통합 테스트는 건너뛴다. 현재 통합 검사는 PostGIS 연결·공간 거리 조회와 Alembic의 모델·DB 비교를 수행하며 데이터를 변경하지 않는다. PostGIS 확장 테이블이 삭제 대상으로 잘못 인식되지 않는지도 확인한다. 데이터 쓰기 테스트를 추가할 때는 별도 테스트 DB와 정리 정책을 먼저 구성한다.

실제 모델 평가 결과와 고정 평가 자료는 [evals](../evals/README.md)에서 별도로 관리한다. pytest 테스트에서 실제 모델·제공처를 자동 호출하지 않는다.
