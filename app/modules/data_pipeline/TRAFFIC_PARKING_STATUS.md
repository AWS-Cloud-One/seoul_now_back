# 시영주차장·차량교통 구현 및 검증 (2026-10-22)

## 요구사항 근거

노션 원문 https://www.notion.so/3f532d4db2e581468a70fb3faf43a224 는 이번 실행에서 접근할 수 없었다. 기존 IMPLEMENTATION_STATUS.md에 정리된 노션 기준(TOPIS 사용, 도로선과 링크 ID 연결, 인구는 추천 순위에서 제외)을 사용했다. 이번 변경이 최신 노션 원문을 전부 검토했다는 뜻은 아니다.

## 사용 API와 선택 이유

| 출처 | 용도 및 현재 상태 |
| --- | --- |
| OA-21709 GetParkingInfo | 시영주차장 총면수·점유대수·원천 갱신시각. 실제 122건 수집. 잔여면수는 총면수-점유대수로 산출. 정수값으로 표현된 소수점 숫자도 처리. 미제공·음수·초과점유·지연은 잔여수 미확인 처리. |
| OA-21285 CITYDATA | 기존 121장소 전체 자동 수집을 재사용. PRK_STTS 1,592개 중복정리 주차 원천을 주변 조회에도 반영. ROAD_TRAFFIC_STTS의 LINK_ID·XYLIST로 도로선 GeoJSON 생성. 사고·통제는 기존 ACDNT_CNTRL_STTS 전체 원문으로 제공. |
| OA-13291 TOPIS TrafficInfo | 도로 링크별 속도·통행시간. XML 수집과 별도 원본 저장 구현. 기존 서울시 키로 1220003800 링크 실제 성공. 원천 측정시각은 응답에 없어 null로 유지하고 수집시각만 기록. |
| OA-15060 서비스링크 보간점 | 서울 전체 도로선 확대 시 추가할 정적 기반 자료. 현재는 도시데이터가 제공하는 선을 사용하며 이 파일 전체 적재는 미구현. TOPIS·도시데이터·표준 링크의 ID를 무조건 같은 것으로 취급하지 않는다. |

공식 출처:
- https://data.seoul.go.kr/dataList/OA-21709/S/1/datasetView.do
- https://data.seoul.go.kr/dataList/OA-21285/A/1/datasetView.do
- https://data.seoul.go.kr/dataList/OA-13291/A/1/datasetView.do
- https://data.seoul.go.kr/dataList/OA-15060/F/1/datasetView.do

## DB 및 데이터 범위

Alembic 0004 추가: provider_snapshots(source PK, data JSONB, collected_at). 시영주차장 원본 122건을 전부 보존한다. 이 API에는 좌표가 없으므로 GetParkInfo 등록정보와 코드가 같고 좌표가 유일한 53건만 공간 테이블에 연결했다. 나머지 69건은 좌표를 추정하지 않고 전체 원본 API에서 제공한다. 갱신 실패 시 마지막 성공 자료를 보존한다.

도시데이터 주차는 매 회차 이후 전체 장소의 최신 PRK_STTS를 중복 정리하여 기존 주차 테이블에 투영한다. 2026-10-22 검증시 1,592건 적재 성공. 주차 잔여면수는 제공표식과 원천시각 15분 이내 조건을 만족할 때만 반환한다. 15분은 프로젝트 표시 정책이며 공식 지연 보증이 아니다.

교통 GeoJSON: 도시데이터에 포함된 주요 장소 주변 4,316개 고유 도로 구간(검증 당시), 광화문·덕수궁 159구간. 서울 전체 도로망을 수집했다는 뜻은 아니다. 원천 좌표 순서(경도, 위도)를 검증하고 잘못된 선은 반환하지 않는다. 동일 링크 중 최신 수집 스냅샷을 선택한다.

TOPIS 두 링크 검증 중 1220003800은 성공, 도시데이터의 1000001401은 제공처 오류 응답이었다. 유사한 ID를 자동 변환하거나 가짜 속도로 채우지 않는다. TOPIS는 지정 링크 단발 수집만 구현했으며 전체 도로 반복 호출은 연결하지 않았다. 전체 도로 확대에는 유효한 TOPIS 대상 링크 목록, ID 매핑, 호출 한도·수집주기 합의가 필요하다.

## 실행 및 확인

- 실행: `docker compose -f compose.yaml -f compose.citydata.yaml up -d citydata-collector municipal-parking-collector`
- 중지: `docker compose -f compose.yaml -f compose.citydata.yaml stop citydata-collector municipal-parking-collector`
- 시영주차장 단발: `docker compose run --rm --no-deps api python -m jobs.collect_municipal_parking`
- 도시데이터 주차 재투영: `docker compose run --rm --no-deps api python -m jobs.project_citydata_parking`
- TOPIS 단발: `docker compose run --rm --no-deps api python -m jobs.collect_topis --link 1220003800`

시영주차장은 회차 종료 후 300초, 도시데이터는 회차 종료 후 600초 대기하고 재수집한다. Docker Desktop과 수집 컨테이너가 실행 중이어야 한다. 기존 서울시 키로 두 신규 서비스를 실제 호출했다. 전용 키가 있으면 APP_SEOUL_MUNICIPAL_PARKING_API_KEY / APP_SEOUL_TOPIS_API_KEY를 우선하며, 비어 있으면 APP_SEOUL_API_KEY를 사용한다. 공공데이터포털 키는 이 서비스들에 사용하지 않는다.

API:
- /api/v1/citydata/inspect : 자료 출처에서 시영주차장 선택. 도로 GeoJSON·전체 원본 링크 추가.
- /api/v1/parking/nearby?lat=37.5665&lon=126.978&source=seoul_municipal_parking
- /api/v1/parking/municipal/raw : 좌표 없는 것 포함 전체 122건
- /api/v1/traffic/roads?area_code=POI009 : 주요장소 주변 GeoJSON
- /api/v1/traffic/roads?limit=1 : total 및 truncated로 반환 범위 확인
- /api/v1/traffic/topis : 저장된 TOPIS 결과
- /api/v1/traffic/topis/1220003800 : 저장된 링크 원본

2026-10-22 실제 확인: 시청 좌표 주변 시영주차장 응답 live_parking, 서울글로벌센터 33면·동국제강 10면(확인 당시 값으로 계속 바뀜). 전체 테스트 43개 및 변경 코드 Ruff 통과, DB migration/model 일치 확인.

## 담당 범위

수집·변환·저장·조회 API와 개발용 점검 화면 변경. React 제품 화면, AI 추천·순위·Bedrock, 카카오 이동시간, AWS 인프라 변경은 없음. 전체 도로망·행정동 공간 연결은 별도 데이터/ID 검증이 필요하다.
