# 도로 ID 매핑 및 프론트 인계 (2026-10-22)

## 구현 범위

제품 프론트엔드 수정 없이 공식 도로선·매핑 파일을 가져와 데이터 처리 및 API에서 결합했다. 공식 최신 제공 파일(확인 당시)은 다음 2025 버전이다. 이를 2026 도로망 전체의 최신 상태라고 표시하지 않는다.

- OA-15060: 서비스링크 보간점 정보(LINK_VERTEX)_2025.xlsx, jobs/data/topis_vertices_2025.xlsx
- OA-15061: 서울특별시 교통소통 표준링크 매핑정보_20250306.xlsx, jobs/data/topis_standard_mapping_20250306.xlsx
- TrafficInfo: TOPIS 서비스 링크별 속도(km/h), 여행시간(초)

숫자가 비슷한 ID를 직접 연결하지 않는다. 도시데이터 LINK_ID를 공식 표준링크→서비스링크 매핑으로 조회한다. 한 서비스 링크가 여러 표준 링크를 포함하므로, TOPIS 속도는 서비스 링크 전체의 관측치로 표시하며 개별 표준 구간의 독립 측정값으로 바꾸지 않는다. 기존 /traffic/roads의 도시데이터 자체 소통값·도로선과 /traffic/network의 서비스링크 도로선·TOPIS 관측은 구분한다.

## 실제 검증 결과

- 공식 파일 5,093개 서비스 링크 중 점 순서 누락(1,2,3,4,5,12)인 1000020300 한 개를 제외, 5,092개 선 적재.
- 완전히 같은 중복 점은 제거. 동일 링크·순번에 서로 다른 좌표가 있으면 적재 실패.
- 자료의 GRS80TM 좌표를 EPSG:5181로 해석해 PostGIS로 WGS84(4326) 변환. 공식 매핑으로 대응하는 도시데이터 선 100개를 지역에 분산해 확인했으며, 두 선 사이 최소거리의 중앙값은 0.07m였다. 이는 자료 간 좌표계 일치 확인이며 위치 정확도 보증이 아니다. 미검증 좌표계의 데이터를 자동 공개하지 않는다.
- 도시데이터의 고유 표준 링크 4,316개 중 2,569개가 공식 매핑과 유효 선에 연결됨. 대응 서비스 링크는 1,477개. 나머지 1,747개는 매핑 보고서에 미연결로 남긴다. 비슷한 ID·끝자리 변환·근접 위치 추정으로 채우지 않는다.
- 광화문·덕수궁: 매핑된 서비스 링크 71개, 선택한 20개 실제 TOPIS 수집 전부 성공. 나머지 51개는 이번 회차에서 호출하지 않았으며 not_collected로 표시.
- 전체 테스트 47개 통과. ID 체계 오연결, 정차(0km/h), 잘못된 숫자, 지연, 최신 실패 상태, 공식 파일 파싱 검사 포함.

## 실행

공식 파일을 갱신한 뒤 정적 자료 재적재:

```powershell
docker compose run --rm --no-deps api python -m jobs.import_topis_geometry
```

공식 매핑으로 해당 장소 주변 서비스 링크를 선택하여 제한 수집:

```powershell
docker compose run --rm --no-deps api python -m jobs.collect_topis_network --area POI009 --max-links 20
```

기본은 한 번 실행한다. `--interval 600`을 명시하면 반복 가능하지만 자동 전체망 실행은 등록하지 않았다. 일일 호출 한도를 확인한 뒤 대상 수와 주기를 정한다. 최대 일일 요청량은 선택 링크 수×86400/간격(실행 시간 제외)이며, 20개·10분 간격이면 약 2,880회다. 기존 citydata·주차 수집기는 기존대로 실행 중이다.

## 프론트·백엔드 전달 계약

- GET /api/v1/traffic/mapping-report : 버전·파일 해시·좌표검증·미연결 ID 목록.
- GET /api/v1/traffic/network?area_code=POI009&limit=100 : 광화문 주변 서비스링크 도로선 + 저장된 TOPIS 관측.
- GET /api/v1/traffic/network?limit=500&offset=0 : 전체 적재 선을 페이징. next_offset이 null일 때 종료.
- GET /api/v1/traffic/roads?area_code=POI009 : 기존 도시데이터 자체 도로선·속도·소통 등급.

GeoJSON의 geometry.coordinates는 [경도,위도] 순서다. 주요 properties:

| 필드 | 의미 |
| --- | --- |
| service_link_id | TOPIS 요청·관측에 사용하는 서비스 링크 ID |
| standard_link_ids | 공식 매핑에서 연결된 표준 링크들의 ID. 숫자가 같다는 이유로 직접 연결하지 않음 |
| geometry_source / source_year / source_crs | 도로선 출처 OA-15060, 2025, 원본 EPSG:5181 |
| speed_kmh | 해당 서비스 링크의 TOPIS 속도. 미수집·지연·실패·잘못된 값은 null, 실제 정차는 0 |
| travel_time_seconds | 해당 서비스 링크 여행시간, 초 |
| observation_status | recently_collected / not_collected / stale / latest_attempt_failed / invalid_observation |
| traffic_collected_at | 우리가 API 결과를 수집한 시각 |
| provider_observed_at | TOPIS 응답에 측정시각이 없어 null. 수집시각을 측정시각으로 만들지 않음 |

최근 수집 결과라도 원천 측정시각의 최신성을 보증할 수 없다. 최근 15분은 프로젝트의 수집시각 표시 정책이다. 측정시각이 제공되는 도시데이터와 구분한다. 최근 요청이 실패하면 과거 정상 원본은 유지하되 현재 값처럼 속도를 반환하지 않는다. 속도 0과 정보없음(null)을 다르게 표시한다.

프론트 담당은 GeoJSON 도로선을 지도에 그리고 상태·속도·시간을 표시하면 된다. 도로등급별 속도 임계값을 합의하지 않은 상태에서 임의로 혼잡 색을 산출하지 않는다. 이번 작업은 React 화면, 추천·순위, AWS 인프라를 변경하지 않았다.

## 남은 조건

전체 도로 실시간 관측은 완료되지 않았다. 전체 대상 링크의 운영 여부, 계정의 호출 한도, 갱신 주기, 미매핑 1,747개에 대한 최신 매핑 자료가 추가로 필요하다. 원본 노션은 이번에도 새로 접근한 것이 아니라 기존 로컬 정리 문서 기준이다.

공식 출처:
https://data.seoul.go.kr/dataList/OA-15060/F/1/datasetView.do
https://data.seoul.go.kr/dataList/OA-15061/F/1/datasetView.do
https://data.seoul.go.kr/dataList/OA-13291/A/1/datasetView.do

## 2026-10-22 TOPIS 주기 수집 활성화

compose.citydata.yaml의 topis-collector를 추가하고 실행했다. 광화문·덕수궁(POI009)의 공식 매핑 서비스 링크 전체를 최대 100개까지 수집하며, 현재 대상은 71개다. 한 회차가 끝난 뒤 300초 대기하고 다시 조회한다. 기존 20개 단발 검증만 수행한 상태에서 이 설정으로 변경됐다. 서울 전체 도로망 반복 수집은 아니다.

실행: `docker compose -f compose.yaml -f compose.citydata.yaml up -d topis-collector`
중지: `docker compose -f compose.yaml -f compose.citydata.yaml stop topis-collector`
로그: `docker compose -f compose.yaml -f compose.citydata.yaml logs --tail=20 topis-collector`

각 요청이 하나의 서비스 링크를 조회하므로 이론상 최대 71×288=20,448회/일(수집 소요시간 제외)이다. 계정 호출 제한에 걸리면 실패 상태를 기록하고 과거 값을 최신 값으로 표시하지 않는다. 호출 한도에 맞춰 대상 수·간격을 조정해야 한다. Docker Desktop 및 컨테이너가 실행 중이어야 한다. API URL을 직접 연 브라우저는 자동 갱신되지 않으므로 새로고침해 최신 응답을 확인한다.

첫 자동 갱신 확인: 광화문 서비스 링크 71개 요청, 70개 관측값 성공 및 1개(1010009100) 제공처 관측값 없음(INFO-200). API는 후자를 no_observation으로 표시하고 속도 null 유지. 신규 상태 분류 테스트 포함 traffic_network 단위 테스트 5개 통과.

## 2026-10-22 전체 도로 범위로 확대

광화문 제한을 제거했다. topis-collector 실행 명령은 `python -m jobs.collect_topis_network --all --interval 300`이다. --all은 검증된 공식 도로선의 모든 서비스 링크(현재 5,092개)를 선택하며, --area와 --max-links의 지역 샘플 제한을 적용하지 않는다. 2025 공식 파일에 포함된 범위이며 최신 서울 전체 도로망을 빠짐없이 포함한다는 보증은 아니다. 순번 누락으로 제외한 1개 링크는 기존 보고서에 남는다.

한 회차에서 제한 병렬 처리(최대 8요청 동시, 요청 시작 간격 최소 0.05초)하며 전체 회차가 끝난 뒤 300초 대기한다. 각 도로가 정확히 5분마다 갱신된다는 뜻은 아니다. 회차 소요시간도 더해진다. 관측값 미제공과 인증·쿼터·통신 실패를 구분하고, 429/ERROR-337/인증 오류가 발생하면 나머지 요청을 멈추고 최소 1시간 대기한다. 마지막 정상 원본은 보존하되 오래된 값을 현재 속도로 표시하지 않는다. 계정 호출 한도는 별도 확인이 필요하다.

수집 진행률: /api/v1/traffic/collection-status
전체 결과: /api/v1/traffic/network?limit=6000
기존 /api/v1/traffic/network?area_code=POI009 는 여전히 광화문 필터를 적용하므로 전체 결과를 보려면 area_code를 제거한다.

진행 상태는 50개 처리마다, 회차 완료·제공처 제한 시 갱신한다. target/completed/success/no_observation/failed/remaining/error_counts를 제공한다. 인증키·전체 요청 URL은 기록하지 않는다.

첫 전체 회차 실제 확인(2026-10-22 KST): target 5,092 / completed 5,092 / speed observations success 5,073 / provider_no_observation 19 / other failures 0 / remaining 0. /traffic/network?limit=6000에서 전체 5,092개 반환, next_offset null 확인. 전체 테스트 50개와 변경 코드 Ruff·diff 검사 통과. 관측값이 없는 19개는 null 유지하며 다음 회차에 재조회한다. 이전 광화문 71개 또는 20개만 수집한다는 설명은 이 후속 전체 범위 설정으로 대체된다.
