# 프론트·백엔드 API 연결 기준

기준: 팀 Notion API 명세서. 모든 경로 앞에는 `/api/v1`을 붙입니다.
개발 화면: http://localhost:5173 / 실행 명세: http://localhost:8000/docs

## 현재 연결 완료

| 기능 | Method / 경로 | 입력 | 응답 |
| --- | --- | --- | --- |
| 도로 교통 | GET /map/traffic | area_code(선택), limit(1~20000) | GeoJSON FeatureCollection |
| 인구 혼잡도 | GET /map/population | area_code(선택) | scope, items: 장소 코드·이름·혼잡도·인구·원본 시각·수집 시각 |
| 주변 주차장 | GET /parkings/nearby | lat, lng, source(선택), limit(1~100) | status, items, 수집 상태·시각과 안내 |
| 카카오 장소 검색 | GET /kakao/places | query, lat, lon, page(선택) | documents, meta |
| 자동차 경로 | GET /kakao/directions | origin_lat, origin_lon, destination_lat, destination_lon | summary(distance: m, duration: 초), coordinates |

기존 /traffic/roads, /citydata/areas, /parking/nearby는 계속 동작합니다.
새 주차장 API는 lng를 받으며 응답에는 호환을 위해 lng와 lon을 모두 제공합니다.
지도 좌표 배열은 [경도, 위도]이며, 카카오 LatLng 생성자는 (위도, 경도)입니다.
주차 빈자리는 real_time_available이 true인 항목만 표시합니다. 미확인은 0면이 아닙니다.
혼잡도는 서울시 주요 장소 데이터이며 행정동 전체 집계가 아닙니다.
실시간 여부를 추측하지 않고 source_time과 collected_at을 구분합니다.

## 미구현: 완료로 표시하지 않을 항목

- GET /map/areas: 노션은 행정동 조회라고 정의하지만 현재 보유 자료는 주요 장소입니다. 행정동 코드·경계의 공식 출처와 버전, GeoJSON 계약을 담당자와 먼저 확정해야 합니다.
- GET /data-status: 관리자용입니다. 관리자 인증/권한 계약을 먼저 정한 뒤 제공처별 최신 성공·실패·수집 시각 응답을 구현합니다. 기존 /parking/runs 및 /traffic/collection-status는 개발 검증용입니다.
- POST /chat/messages: AI 제공처·프롬프트·요청/응답·대화 저장·추천 근거 계약이 미정입니다. AI 담당 구현 후 연결합니다. 임의의 성공 응답은 만들지 않습니다.

## 담당별 다음 작업

- 데이터 담당: 행정동 자료 확보, 데이터 출처·갱신 기준·누락 의미 확인.
- 백엔드 담당: 관리자 인증, 공통 오류/응답 스키마 확정, 필요한 DB·서비스 구현.
- AI 담당: 실제 모델 연결, 추천 후보/설명 검증, 챗봇 계약 정의.
- 프론트 담당: 위 계약으로 검색·지도 UI 연결. 관리자·챗봇은 구현 및 계약 확정 후 추가.

## 오류와 키

422: 입력 오류 / 404: 해당 자료 또는 경로 없음 / 502: 카카오 연결·권한 오류 /
503: 키 미설정 또는 사용 한도 / 504: 제공처 시간 초과.
JavaScript 키는 프론트 VITE_KAKAO_MAP_APP_KEY, REST 키는 백엔드 APP_KAKAO_REST_API_KEY.
REST 키는 브라우저 코드에 전달하지 않습니다. 실제 환경변수는 Git에 추가하지 않습니다.

## 추가 데이터 연결 (2026-10-25)

- GET /api/v1/map/areas: SGIS 행정동 경계, district로 구 이름 필터. SGIS 코드이며 카카오·행안부 코드와 임의 결합 금지. 조회 응답의 경계 기준연도는 미제공.
- GET /api/v1/map/pedestrians: 서울 경계 내 S-DoT 설치 지점. count_10min은 최근 30분 이내 관측만 제공하며 0을 보존. 이전 값은 last_count_10min, 없는 값은 null. 이는 통행 횟수이며 사람 수나 혼잡 등급이 아님.
- GET /api/v1/map/cctv: 저장된 ITS 목록. 재생·영상 갱신은 미검증. 24시간 경과 URL은 제거. 조회·클릭으로 ITS 목록 API를 재호출하지 않음. 서울 필터는 SGIS 2025 통계 경계 기준.
- GET /api/v1/parkings/national/nearby?lat=...&lng=...: 교통안전공단 시설·운영·현황을 관리 ID로 결합. 기능별 최대 5000행 부분 수집. 측정 시각 없는 주차 현황은 검증된 현재 빈자리로 사용하지 않음.

명시적 수집 명령 (백엔드 폴더에서 실행):

```powershell
docker compose exec -T api python -m jobs.collect_supplemental areas
docker compose exec -T api python -m jobs.collect_supplemental sdot
docker compose exec -T api python -m jobs.collect_supplemental parking
# 다음 명령은 ITS 한도를 실제 소비하므로 필요할 때만 실행
docker compose exec -T api python -m jobs.collect_supplemental cctv
```

자동 수집은 아직 설정하지 않았습니다. S-DoT는 최근 10000행 범위이므로 과거 전체 수집이 아니며, 누락 지점은 센서 고장으로 확정하지 않습니다.
CCTV HLS 재생은 브라우저에 따라 지원되지 않을 수 있습니다. 현재 제공 URL 열기 기능이며 내장 플레이어·재생 검증은 후속 작업입니다.
과거 문서의 /map/areas 미구현 표시는 위 구현으로 대체합니다. 관리자 /data-status·AI 챗봇·121장소 관측 다각형은 이번 추가 범위에서 아직 미구현입니다.

## 지도 보완 및 자동 갱신

- 공식 서울 121장소 SHP를 WGS84로 적재했습니다. 출처는 서울 도시데이터 안내의 주요 121장소 다운로드입니다. /map/population/geometry에서 실제 관측 경계와 인구를 장소 코드로 결합합니다.
- 대체값(Y), 원본 시각 없음·미확인, 미래 시각, 1시간 초과값은 혼잡 색상을 주지 않습니다. 1시간은 앱의 표시 정책이며 제공처 실시간 보증이 아닙니다.
- TOPIS 도로망은 파랑(최근 관측 있음)과 회색 점선(미확인·지연)을 구분합니다. 속도만으로 임의 혼잡 등급을 만들지 않습니다. 도시데이터 도로는 별도 선택합니다.
- CCTV 플레이어는 클릭 시 한 카메라만 연결하며 닫거나 레이어를 바꾸면 재생을 중지합니다. 영상 촬영 시각·절대 지연과 모든 카메라 재생은 검증하지 않았습니다.
- supplemental-collector: S-DoT 10분, 주차 부분 수집 24시간, 행정동 7일 간격. 재시작 시 S-DoT·주차를 다시 수집합니다. ITS 자동 조회는 없습니다.
- 실행: docker compose -f compose.yaml -f compose.citydata.yaml up -d supplemental-collector
- 중지: docker compose -f compose.yaml -f compose.citydata.yaml stop supplemental-collector
- S-DoT 최신 관측의 실제 지연이 표시 정책보다 크면 미확인으로 보입니다. 수집 성공을 측정 시각 갱신으로 해석하지 않습니다.
- 주차 시설·운영정보는 여전히 기능별 최대 5000행 부분 수집입니다. 현재 제공처 totalCount와 서울 필터의 관계를 확인하기 전 전체 수집 완료로 표시하지 않습니다.
