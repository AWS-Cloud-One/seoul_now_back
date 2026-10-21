# 단발 수집 실행 진입점

데이터 처리 담당이 작성하고 AWS 담당이 실행 명령·종료 상태를 연결한다. API와 같은 코드·설정·DB 모델을 사용하되 별도 프로세스로 실행한다.

- `python -m jobs.collect_parking`: 서울 공영주차장 등록 기본정보 수집. `.env`의 `APP_SEOUL_API_KEY` 사용.
- `python -m jobs.collect_citydata_parking --area "광화문·덕수궁"`: OA-21285의 주차 섹션만 기존 주차 테이블에 적재.
- `python -m jobs.collect_citydata --area "광화문·덕수궁"`: OA-21285 응답 전체를 장소별 최신 스냅샷으로 저장. 유동인구, 도로소통, 대중교통, 주차, 사고·통제, 따릉이, 날씨, 충전소, 행사, 상권 등 응답에 포함된 모든 섹션을 보존한다. `.env`의 `APP_SEOUL_CITYDATA_API_KEY` 사용.

Citydata는 요청 1회에 장소 1곳을 조회한다. 필요한 장소별로 명령을 실행하며, 121개소 전체를 무차별 반복 호출하지 않는다. 새 섹션/필드가 제공돼도 원본 JSON 전체를 보관하므로 정규화 변경 없이 API 조회가 가능하다.

진입점 담당 순서:

1. 실행 인자와 `app/core/config.py` 설정을 읽는다.
2. `app/db/session.py`의 `create_engine()`으로 작업 전용 DB 연결을 만든다.
3. HTTP 클라이언트를 열고 `app/modules/data_pipeline/pipelines/`의 회차 처리를 호출한다.
4. 민감정보를 제외한 결과를 JSON 한 줄로 출력한다.
5. 클라이언트와 엔진을 닫고 성공/실패 종료 코드를 반환한다.

DB 마이그레이션은 기존 루트 `migrations/`와 Alembic 명령으로 별도 적용한다. AWS 스케줄 연결은 수집 주기와 호출 한도를 합의한 뒤 구성한다.

## 전체 장소 자동 수집 (2026-10-22)

서울시 공식 OA-21285 첨부파일(목록 seq=23)을 jobs/data/seoul_areas.xlsx에 보관했다. jobs.collect_citydata_all은 이를 읽어 121개 공식 장소를 각각 요청한다. 인증키는 기존 APP_SEOUL_CITYDATA_API_KEY를 사용하며 키를 출력하지 않는다.

실행: `docker compose -f compose.yaml -f compose.citydata.yaml up -d citydata-collector`
중지: `docker compose -f compose.yaml -f compose.citydata.yaml stop citydata-collector`
단발 전체 수집: `docker compose run --rm --no-deps api python -m jobs.collect_citydata_all`

수집 컨테이너는 한 회차 종료 후 600초 대기하고 다음 회차를 시작한다. 전체 요청 수는 하루 최대 약 17,424회(121×144회, 실행 소요시간 제외)로 계정의 호출 한도에 따라 간격을 늘려야 한다. 재시도는 추가 호출이다. 화면은 60초마다 수집된 목록을 새로 읽으며 현재 선택을 유지한다. 원천 기준 시각과 수집 시각은 다르며, 원천에서 없는 값은 그대로 유지한다. Docker Desktop이 켜져 있어야 자동 수집이 동작한다. 이는 로컬 수집 작업이며 AWS 서비스 변경은 없다.

## 시영주차장 및 TOPIS

시영주차장 `python -m jobs.collect_municipal_parking`: 원본 전체를 provider_snapshots에 보존하고 좌표가 유일하게 연결된 기록을 주변 조회에 적재. `--interval 300`으로 반복 실행. 전체 자동 수집은 compose.citydata.yaml의 municipal-parking-collector 사용.

TOPIS `python -m jobs.collect_topis --link 1220003800` (여러 --link 허용): XML 응답을 별도 저장. 전체 도로 반복 호출 아님. 상세 검증과 범위는 app/modules/data_pipeline/TRAFFIC_PARKING_STATUS.md 참조.

## 공식 도로선·ID 매핑

`python -m jobs.import_topis_geometry`는 jobs/data의 공식 2025 도로선·표준 매핑 XLSX를 검증하고 적재한다.
`python -m jobs.collect_topis_network --area POI009 --max-links 20`은 도시데이터 장소를 공식 매핑으로 TOPIS 서비스 링크에 연결해 제한 수집한다. 기본은 단발이며 반복은 --interval 명시. 전체 자동 수집 등록 전에 호출 한도 확인. 상세 필드와 검증: app/modules/data_pipeline/TRAFFIC_MAPPING_HANDOFF.md.
