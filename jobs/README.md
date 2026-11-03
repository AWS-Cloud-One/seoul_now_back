# 단발 수집 실행 진입점

데이터 처리 담당이 작성하고 AWS 담당이 실행 명령·종료 상태를 연결한다. API와 같은 코드·설정·DB 모델을 사용하되 별도 프로세스로 실행한다.

- `python -m jobs.collect_parking`: 서울 공영주차장 등록 기본정보 수집. `.env`의 `APP_SEOUL_API_KEY` 사용.
- `python -m jobs.collect_citydata --area "광화문·덕수궁"`: OA-21285 응답 전체를 장소별 최신 스냅샷으로 저장. 유동인구, 도로소통, 대중교통, 주차, 사고·통제, 따릉이, 날씨, 충전소, 행사, 상권 등 응답에 포함된 모든 섹션을 보존한다. `.env`의 `APP_SEOUL_CITYDATA_API_KEY` 사용.

Citydata는 요청 1회에 장소 1곳을 조회한다. 개별 장소 점검은 `collect_citydata`, 전체 수집은 아래 `collect_citydata_all`을 사용한다. 전체 수집 후 `project_citydata_parking`이 저장된 도시데이터의 주차 정보를 반영한다. 별도 주차 전용 단일 지역 수집 명령은 사용하지 않는다. 새 섹션/필드가 제공돼도 원본 JSON 전체를 보관하므로 정규화 변경 없이 API 조회가 가능하다.

진입점 담당 순서:

1. 실행 인자와 `app/core/config.py` 설정을 읽는다.
2. `app/db/session.py`의 `create_engine()`으로 작업 전용 DB 연결을 만든다.
3. HTTP 클라이언트를 열고 `app/modules/data_pipeline/pipelines/`의 회차 처리를 호출한다.
4. 민감정보를 제외한 결과를 JSON 한 줄로 출력한다.
5. 클라이언트와 엔진을 닫고 성공/실패 종료 코드를 반환한다.

저장 작업은 수집 원문뿐 아니라 정규화 결과까지 확인한다. 정규화 실패 시 원문·실패 기록을 커밋한 후 실패를 알리며, 부분 처리도 성공으로 보고하지 않는다. 단발 명령의 종료 코드는 성공 `0`, 실패 `1`, 부분 처리 `2`다. 잠금으로 건너뛴 수집은 `3`, 제공처 제한으로 중단한 TOPIS 회차는 `4`를 사용한다.

모든 실행 모듈은 동기 `main()`에서 CLI 인자를 처리하고 비동기 `execute()`에서 자원 준비·결과 출력 후 정수 종료 코드를 반환한다. `jobs/runtime.py`가 JSON 한 줄 출력, 상태별 종료 코드, 실행 예외와 사용자 중단(`130`)을 공통 처리한다. 파이프라인은 처리 결과를 반환하며 직접 프로세스를 종료하지 않는다. 스케줄러는 개별 작업 코드와 실행 조건을 종합해 단발 실행의 성공 여부를 반환한다.

공식 자료 경로는 `REFERENCE_DATA`(`data/reference/`), 로컬 파일 경로는 `RAW_DATA`(`data/raw/`)를 사용하며 현재 작업 디렉터리에 의존하지 않는다. 기본 원문 저장소는 `data/raw/payloads`이며 Compose가 `data/raw/`를 컨테이너에 연결한다. 원문 저장 디렉터리는 저장 시 자동으로 생성된다. `data/`는 공식 XLSX·ZIP 입력 파일만 Git과 Docker 빌드에 포함한다. 모든 명령은 `--help`로 인자를 확인할 수 있다.

공통 스냅샷 저장은 `app.modules.data_pipeline.pipelines.snapshots.save_snapshot`을 호출한다. 저장 검증은 `tests/integration/`에서 실제 테이블과 원문 보관 파일을 확인한다.

`python -m jobs.collect_supplemental areas|sdot|cctv|parking`은 설정·자원을 준비하고 `pipelines/supplemental.py`를 호출한다. 외부 요청은 `collectors/supplemental.py`, 값 변환은 `normalizers/supplemental.py`, 좌표 변환·서울 경계 포함 조회는 `SpatialRepository`가 담당한다. S-DoT·CCTV는 SGIS 서울 경계가 없으면 외부 API 호출 전에 실패한다. HTTP 대기 중 DB 트랜잭션은 열어두지 않는다.

기존 `cctv --reuse-probe` 옵션은 `data/raw/its_probe.json`이 있을 때 요청 없이 파일과 수정 시각을 재사용한다. 이 파일은 공식 입력 파일이나 자동 수집의 선행 조건이 아니다.

DB 마이그레이션은 기존 루트 `migrations/`와 Alembic 명령으로 별도 적용한다. AWS 스케줄 연결은 수집 주기와 호출 한도를 합의한 뒤 구성한다.

## 자동 수집

서울시 공식 OA-21285 첨부파일(목록 seq=23)을 data/reference/seoul_areas.xlsx에 보관했다. jobs.collect_citydata_all은 이를 읽어 121개 공식 장소를 각각 요청한다. 인증키는 기존 APP_SEOUL_CITYDATA_API_KEY를 사용하며 키를 출력하지 않는다.

도시데이터·주차 명령은 실행 인자·설정·출력만 담당하며 실제 회차 처리는 `app/modules/data_pipeline/pipelines/`에 있다. 공식 장소 목록은 `collectors/citydata.py`에서 읽고, 시영주차 좌표 조회는 `ParkingRepository`, 유일 좌표 연결은 `normalizers/parking.py`에서 처리한다.

전체 수집의 마지막 JSON에서 `success`·`partial`·`failed`·`skipped`는 장소별 수집 건수이고 `parking_projection`은 주차 반영 결과다. 최상위 `status`와 종료 코드는 두 단계를 함께 판단한다. 어느 단계든 실패하면 실패, 전부 성공하면 성공, 전부 건너뛰면 건너뜀, 그 밖에는 부분 처리다.

실행: `docker compose up -d citydata-collector`
중지: `docker compose stop citydata-collector`
단발 전체 수집: `docker compose run --rm --no-deps api python -m jobs.collect_citydata_all`

서비스 이름은 `citydata-collector`지만 실제로는 `jobs.scheduler`가 아래 작업을 함께 실행한다. 별도 수집 Compose 파일은 사용하지 않는다.

| 작업 | 기본 주기 | 선행 조건 |
| --- | --- | --- |
| 도시데이터 121장소 | 300초 | 서울 도시데이터 키 |
| 주차 등록정보 | 86400초 | 서울시 키 |
| 시영주차장 | 300초 | 시영주차장 키 또는 서울시 키 |
| SGIS 행정동·서울 경계 | 604800초 | SGIS 키·시크릿 |
| S-DoT | 600초 | S-DoT 키와 SGIS 서울 경계 |
| 전국 주차정보 | 300초 | 공공데이터포털 키 |
| ITS CCTV 목록 | 3600초 | ITS 키와 SGIS 서울 경계 |
| TOPIS | 300초, 최대 200개 링크씩 순환 | TOPIS 키 또는 서울시 키와 정형 적재된 공식 도로 자료 |

주기는 회차 시작 기준이며 작업이 길어지면 완료 후 최소 1초 대기한다. 필요한 키가 없으면 건너뛰고, 선행 경계·도로 자료가 없으면 기다린다. TOPIS 실패 후 재시도는 최소 1시간 간격이다. 실제 주기와 키 조건은 [scheduler.py](scheduler.py)의 `TASKS`에서 관리하며 제공처 호출 한도를 확인한 뒤 조정한다. 같은 작업의 개별 반복 실행과 스케줄러를 동시에 켜지 않는다. 원천 기준 시각과 수집 시각은 구분하며 Docker Desktop이 켜져 있어야 자동 수집이 동작한다.

선행 경계·도로 확인은 `ReferenceRepository.has_prerequisite()`로 정형 테이블과 적재 메타데이터를 조회한다. 원본 파일 전체를 열지 않으며, 마지막 정상 적재본에 연결된 활성 행이 있어야 실행한다. 실패한 재적재 후에는 기존 정상 자료를 사용할 수 있지만, 정상 빈 적재본이나 비활성 자료를 이전 행으로 대신하지 않는다. 이 조회 전환은 `TASKS`의 수집 주기·대상 수·실패 후 대기를 변경하지 않는다.

## 시영주차장 및 TOPIS

시영주차장 `python -m jobs.collect_municipal_parking`: 원본 전체를 파일에 회차별 보존하고 `raw_payloads`에 참조를 기록하며 좌표가 유일하게 연결된 기록을 주변 조회에 적재. 개별 명령은 `--interval 300`으로 반복할 수 있으며 Compose 자동 수집은 위 공통 스케줄러를 사용한다.

TOPIS `python -m jobs.collect_topis --link 1220003800` (여러 --link 허용): XML 응답을 별도 저장. 전체 도로 반복 호출 아님. TOPIS 서비스 링크별 관측값이며 도시데이터의 표준 링크 ID와 직접 연결하지 않는다.

`collect_topis`와 `collect_topis_network`는 설정·HTTP/DB 자원·출력·종료 코드를 맡고, 서로를 가져오지 않는다. 공통 수집은 `pipelines/topis.py`, 대상 선정·회차 결과 규칙은 `pipelines/topis_rules.py`, TOPIS 요청은 기존 `collectors/topis.py`에서 처리한다. `collect_topis_network`의 명시적 반복 대기만 실행 파일에 남는다.

자동 수집의 `--all --scheduled-batch --max-links 200`은 미시도·오래된 시도 순서로 최대 200개를 선택한다. 수동 `--all` 단독은 검증된 도로 전체를 선택하며 `--max-links`로 제한되지 않는다. 요청 중 한도·인증 오류가 발생하면 새 요청을 중단하고 종료 코드 `4`를 반환하며, 명시적 반복은 최소 1시간 뒤 재개한다. 관측 없음만 발생한 회차는 부분 처리 `2`, 그 외 실패는 `1`이다. 스케줄러의 주기·실패 후 대기 설정은 유지한다.

회차 마지막 JSON은 기존 `cycle_complete`·`target`·`counts`에 최종 `status`를 함께 출력한다. `cycle_complete`는 선택한 대상 처리가 끝났는지를 뜻하며 전부 성공했다는 의미는 아니다. 저장된 진행 보고서의 `running`·`complete`·`paused_provider_limit` 및 기존 집계 필드는 유지한다.

TOPIS 전체·자동 순환은 도로 ID·시도 기록을 DB에서 직접 조회한다. 과거 장소 지정 수집 옵션은 제거했으며, 호출할 도로를 고르기 위해 도시데이터 원문이나 표준 ID 매핑을 읽지 않는다. 도시데이터 자체의 장소별 수집, 주차 재처리·실시간 주차 정보와 API 응답 처리는 유지한다.

## 공식 도로선·ID 매핑

`python -m jobs.import_topis_geometry`는 data/reference의 공식 2025 도로선·표준 매핑 XLSX를 검증하고 적재한다.
`python -m jobs.import_place_boundaries`는 공식 121장소 SHP 경계를 내부 링까지 보존해 적재한다.

두 명령은 파일 경로·설정·자원·출력만 맡으며, 가져오기 흐름은 `pipelines/official_files.py`, 파일 해석은 `collectors/official_files.py`, 값 검증은 `normalizers/official_files.py`, 공간 SQL은 `SpatialRepository`에 있다. TOPIS의 좌표 교차 검증 실패는 `coordinate_cross_validation_failed`와 표본 수·거리 중앙값을 출력하고 종료 코드 `1`을 반환한다. 이 경우 기존 도로선은 갱신하지 않는다. 검증 후 정형 저장이 실패하면 공통 저장 흐름에서 원문·실패 회차를 보존한다.

`python -m jobs.collect_topis_network --all`은 공식 도로 전체를 한 번 수집한다. `--all --scheduled-batch --max-links 20`은 미시도·오래된 도로부터 최대 20개만 수집한다. 기본은 단발이며 반복은 `--interval`로 명시한다. 공식 자료의 출처·버전·연결 규칙은 아래 [공식 입력 자료](#공식-입력-자료)를 참고한다.


DB head 0009에서 수집 원문은 불변 파일로 보존하고 `raw_payloads`에는 경로·해시·크기를 기록한다. 핵심 값과 제외 사유는 정형 테이블에 적재한다. API·작업·마이그레이션의 `APP_PAYLOAD_STORAGE_DIR`는 같은 저장소여야 한다.
`project_citydata_parking`은 새 수집으로 기록하지 않으며 원본 payload와 수신 시각을 유지한다.
주차 반영 도중 정규화가 실패하면 해당 반영의 DB 쓰기를 되돌리고 원본 연결과 실패 회차를 보존한다. 단독 실행도 부분 처리·실패를 종료 코드에 반영한다.
`publish_map`은 정형 테이블을 읽어 공개본을 만든다. 자세한 적용 순서는
[백엔드 README의 DB 변경 절차](../README.md#새-기능을-추가할-때)를 참고한다.
공개 완료 시 `{"status":"success","layer":"roads","features":123}` 형식의 JSON을 출력하고 종료 코드 `0`을 반환한다. `features`는 해당 실행이 공개한 개수다.

## 공식 입력 자료

`data/reference/`에는 수집 대상과 공간 기준정보를 만드는 고정 입력 파일을 보관한다. 기존 `jobs/data/`에서 내용 변경 없이 옮긴 자료이며 Git으로 공유하고 Docker 이미지에 포함한다. 아래 기준일은 기존 파일명·구현 기록에 따른다. 갱신 시 출처·기준일과 실행 경로를 함께 갱신한다.

| 파일 | 내용·출처 | 사용하는 작업 |
| --- | --- | --- |
| `seoul_areas.xlsx` | OA-21285 공식 121장소 목록, 기존 기록의 첨부 seq=23 | `collect_citydata_all` |
| `citydata_places_official.zip` | 기존 저장소의 공식 121장소 경계 SHP 묶음 | `import_place_boundaries` |
| `sdot_locations_20251113.xlsx` | 기존 저장소의 S-DoT 설치 위치 목록, 파일명 기준 2025-11-13 | `collect_supplemental sdot` |
| `topis_vertices_2025.xlsx` | OA-15060 서비스링크 보간점, 2025 자료 | `import_topis_geometry` |
| `topis_standard_mapping_20250306.xlsx` | OA-15061 표준링크·서비스링크 매핑, 2025-03-06 자료 | `import_topis_geometry` |

- TOPIS 속도·통행시간은 서비스 링크 전체의 관측값이다. 도시데이터의 표준 링크와는 공식 매핑으로 연결하며 비슷한 번호나 인접 위치로 ID를 추정하지 않는다.
- 도로 보간점은 EPSG:5181에서 WGS84(4326)로 변환한다. 정점 순서 누락·충돌을 검사하고 공식 매핑으로 연결한 도시데이터 도로선과 좌표를 교차 검증한 뒤 적재한다.
- 좌표 교차 검증은 최대 100개 표본 중 10개 이상이 필요하며 거리의 상위 중앙값이 30m 이하여야 한다. 검증 실패 시 기존 도로선을 바꾸지 않는다.
- 장소 경계는 121개 고유 장소와 좌표계를 확인하고 내부 링도 보존한다. S-DoT 위치와 최신 관측은 센서 ID로 연결한다.

팀과 공유할 작은 검증 표본은 `tests/fixtures/`에 출처·시각·가공 내용을 기록해서 추가한다. 실행 중 생성되는 원문과 로컬 백업은 Git·Docker 빌드에서 제외한다.
