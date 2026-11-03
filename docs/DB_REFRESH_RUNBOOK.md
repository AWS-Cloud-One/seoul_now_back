# DB 저장·자동 갱신 운영 절차

## 저장 기준

| 테이블 | 고유 식별자 | 보관 방식 |
| --- | --- | --- |
| citydata_snapshots | 공식 AREA_CD | 장소별 최신 전체 JSON, collected_at이 더 늦은 회차만 갱신 |
| parking_records | 출처·원천 코드·이름·주소·좌표의 해시 | 원천 구획별 최신 정규화 값, PostGIS 좌표 |
| provider_snapshots | 출처 코드 | 제공처별 최신 JSON 및 마지막 시도 상태 |
| collection_runs | 회차 UUID | 성공·부분 성공·실패, 건수, 안전한 오류 코드 |
| external_api_responses | 응답 UUID, 회차 외래 키 | 인증 항목을 제거한 수집 결과 JSON 이력 |
| external_api_observations | 응답 UUID + record_key | 응답 안에서 중복 없는 관측 기록 |

같은 주차장 코드에 여러 구획이 존재하므로 코드 하나만으로 병합하지 않는다.
이름·주소·좌표가 바뀌면 새로운 원천 기록으로 남는다. 물리적 시설 통합과 폐쇄된 구획의
삭제는 별도 식별 정책이 필요하다. 최신값은 반복 수집해도 같은 식별자로 갱신되고,
이력 테이블은 성공한 회차마다 늘어나는 것이 정상이다.

도시데이터는 장소 전체를 한 관측 기록으로 저장한다. 인구·교통 등 섹션별 시각은 원문에
보존하며 하나의 대표 관측 시각으로 추정하지 않는다. 주차 관측값은 정규화된 기록별로
저장한다. 기타 제공처는 전체 스냅샷 단위 이력이며 센서별 시계열 정규화는 하지 않는다.
실제 HTTP 응답의 각 페이지가 아니라 수집기가 조립한 결과를 보관한다.

도시데이터·주차 수집의 최신값 갱신, 이력 저장, 성공 기록은 같은 트랜잭션이다.
어느 하나라도 실패하면 함께 롤백하고 실패 결과를 별도 트랜잭션에 남긴다.
제공처 스냅샷도 최신값과 이력을 같은 트랜잭션으로 저장한다.
수집 시각과 원천 관측 시각을 구분하며 원천 시각이 없으면 NULL을 유지한다.

이력은 자동 삭제하지 않는다. 운영 보관 기간과 저장 용량은 배포 전에 합의해야 한다.
`external_api_observations` → `external_api_responses` → `collection_runs` 순서로
참조를 해제해야 하며 최신값 테이블은 이력 정리 대상으로 포함하지 않는다.

## 적용 순서

개발 변경은 `codex/db-refresh-integration` 브랜치에서 진행한다.
모델 변경자와 검토자가 마이그레이션을 확인하고, 운영에서는 지정한 배포 담당자 한 명 또는
단일 release job이 실행한다. 각 API replica의 시작 명령에 마이그레이션을 넣지 않는다.
동시에 실행하면 DB advisory lock을 얻지 못한 마이그레이션이 종료되므로 완료 후 재시도한다.

1. DB 백업과 대상 환경을 확인한다. 적용된 migration 파일을 삭제하거나 수정하지 않는다.
2. 아래 격리 검증 환경에서 새 DB 생성·적용·모델 비교·통합 테스트를 실행한다.
3. 개발 DB에 `alembic upgrade head`를 적용하고 `alembic current`, `alembic check`를 확인한다.
4. API와 수집 worker를 실행한다. Compose는 DB 정상 → migrate 완료 → API/worker 순서를 보장한다.
5. 로그와 조회 API에서 회차 및 최신 데이터를 확인한다.

현재 migration head는 `0008`이다. 기존 개발 DB의 `0006` 인구 테이블과 `0007` 미사용 이력
정리 이력을 보존했다. `0008`에서 수집 코드가 사용하는 이력 테이블을 다시 만들고 최근 회차
조회 인덱스를 추가한다. `0007`에서 `0008`로 적용하는 SQL은 `migrations/upgrade_review.sql`에 있다.
이미 테이블이 있는 환경에 전체 SQL을 수동 실행하지 말고 Alembic으로 필요한 revision만 적용한다.

```powershell
# 기존 서비스 DB와 볼륨을 공유하지 않는 검증
docker compose -p seoul-now-refresh-validation -f compose.validation.yaml run --build --rm tests

# 개발 환경 시작: migrate 완료 후 API와 자동 수집 실행
docker compose up --build -d
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic check
docker compose logs --tail 30 citydata-collector

# 단발 실행: 실패하면 종료 코드 1
docker compose run --rm --no-deps api python -m jobs.scheduler --once
```

`compose.validation.yaml`은 API 키를 전달하지 않으며 외부 API도 호출하지 않는다.
호스트 Python은 `.env`의 `APP_DATABASE_URL`을 사용한다. Docker Compose는 내부 DB 서비스
주소를 기본으로 사용하며, 컨테이너 DB를 따로 지정하려면 `DOCKER_DATABASE_URL`을 설정한다.
DB 통합 테스트는 중복 제거·변경 반영·실패 시 이전값 보존·저장 롤백·공간 조회·모델 일치를 검사한다.
테스트 자료만 생성·정리한다. 운영 DB를 TEST_DATABASE_URL로 지정하지 않는다.

## 자동 수집

기존 Compose 서비스명 `citydata-collector`는 공통 scheduler worker를 실행한다.
API 프로세스를 늘려도 worker가 API 내부에서 추가 생성되지 않는다.

| 작업 | 기본 주기 | 필요한 설정 |
| --- | --- | --- |
| citydata | 회차 시작 기준 300초 | APP_SEOUL_CITYDATA_API_KEY |
| parking | 86400초 | APP_SEOUL_API_KEY |
| municipal | 300초 | APP_SEOUL_MUNICIPAL_PARKING_API_KEY 또는 APP_SEOUL_API_KEY |
| sdot | 600초 | APP_SEOUL_SDOT_API_KEY 및 SGIS 서울 경계 |
| national-parking | 300초 | APP_DATA_GO_KR_API_KEY |
| areas | 604800초 | APP_SGIS_CONSUMER_KEY와 APP_SGIS_CONSUMER_SECRET 모두 필요 |
| cctv | 3600초 | APP_ITS_API_KEY 및 SGIS 서울 경계 |
| topis | 300초, 최대 200개 링크 순환 | APP_SEOUL_TOPIS_API_KEY 또는 APP_SEOUL_API_KEY 및 공식 도로 매핑 |

위 주기는 코드의 시작 설정이며 제공처의 승인 쿼터·갱신 정책 확인 후 `jobs/scheduler.py`의
TASKS에서 조정한다. 기존 바탕화면 프로젝트에서 누락된 키 설정을 현재 로컬 `.env`로 가져왔다.
키 값은 로그·문서·Git에 기록하지 않는다. 위 8개 작업은 모두 기본 실행 대상이다.
키가 없는 작업은 skipped 로그를 남긴다. 모든 작업의 키가 없으면 worker가 종료된다.
작업 시간이 주기보다 길면 해당 회차 완료 뒤 최소 1초 대기하며 같은 worker에서 겹치지 않는다.
작업별 DB advisory lock으로 여러 scheduler의 동시 실행도 방지한다.
회차의 하위 수집기는 기존 출처별 잠금을 사용한다. 작업당 30분 시간 제한 후 자식 프로세스를
종료하고 다음 주기에 재시도한다. 작업별 실패가 다른 데이터 수집을 중단시키지 않는다.

```powershell
docker compose run --rm --no-deps api python -m jobs.scheduler --task sdot --task national-parking --once
```

기존 `compose.citydata.yaml`의 개별 loop와 공통 scheduler를 함께 운영하지 않는다.
TOPIS는 미조회 또는 가장 오래전에 시도한 링크부터 최대 200개씩 조회한다. 5,092개 링크를
한 번에 요청하지 않는다. 제공처 제한·실패 시 다음 시도까지 최소 1시간 대기한다.
관측값 없음은 부분 수집으로 기록한다. SGIS 경계나 도로 매핑이 없으면 해당 작업은
60초 간격으로 선행 자료를 기다린다. SGIS 작업은 경계를 함께 갱신한다.
CCTV 주기는 영상 재생 프레임이 아닌 카메라 목록·영상 주소 갱신 주기다.
카카오 장소 검색·길찾기는 검색어와 출발지·목적지가 필요한 사용자 요청형 API로 유지한다.

AWS에서는 같은 이미지의 `python -m jobs.scheduler`를 별도 worker 서비스로 실행하거나,
예약 실행 서비스에서 `python -m jobs.scheduler --once`를 호출할 수 있다.
두 방식을 같은 작업에 동시에 설정하지 않는다. ECS/EKS 선택, IAM, 네트워크, 백업 및
배포 도메인은 아직 확정하지 않았고 실제 클라우드 배포는 수행하지 않았다.

## 프론트엔드 API 계약

기존 API 형식은 유지한다. 조회 요청은 DB만 읽고 외부 수집을 시작하지 않는다.

| GET 경로 | 용도·주요 응답 |
| --- | --- |
| /api/v1/map/population | items: area_code, area_name, collected_at, source_time, population |
| /api/v1/map/traffic | 수집된 도로 GeoJSON, 좌표 순서는 경도·위도 |
| /api/v1/parkings/nearby?lat=37.5665&lng=126.978 | items와 주차 최신성·가용 여부 |
| /api/v1/citydata/areas/{area_code} | 장소 전체 JSON과 collected_at, 미수집 404 |
| /api/v1/collections/runs?source=seoul_citydata&limit=50 | 최근 회차 items, source는 생략 가능, limit은 1..200 |

회차 응답에는 id, source, area_code, area_name, started_at, finished_at, status,
received, accepted, rejected, duplicates, error_code, rejection_reasons가 포함된다.
status는 기존 running/success/partial/failed이며 진행 중 finished_at은 null이다.
수집 시간이 최근이어도 원천 관측 시각이 오래됐으면 실시간 정보로 표시하지 않는다.
강제 종료된 회차는 running으로 남을 수 있으므로 finished_at과 서버 로그를 함께 확인한다.

입력 검증 실패는 422, 조회 자원 없음은 404, 미수집 보조 데이터는 503이다.
오류의 detail은 문자열 또는 FastAPI 검증 오류 배열일 수 있다. OpenAPI는 개발 서버의
`/openapi.json`에서 확인한다. 프론트는 문자열이 아닌 detail에 공통 오류 문구를 사용한다.
브라우저 연동 시 APP_CORS_ORIGINS와 지도 JavaScript 키의 허용 도메인을 실제 배포 주소에 맞춘다.

## 이번 검증과 남은 외부 확인

API·단위 테스트와 Ruff를 실행했고 Alembic 전체 SQL 생성도 확인했다.
실제 적용 결과와 DB 백업 위치는 `docs/DB_REFRESH_VERIFICATION.md`에 기록한다.
공공 API 실수집 성공 여부는 인증·쿼터·제공처 상태를 포함해 단발 실행 결과로 확인한다.
Bedrock과 수치 추천 비교는 같은 후보·입력을 고정하고 응답 시간, 추천 근거와 순위를 평가해야 한다.
현재 추천 구현·모델 접근 설정이 없어 실제 비교 결과는 생성하지 않았다.
