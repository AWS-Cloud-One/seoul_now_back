# 김초래 데이터 처리 구현 현황 및 인계 문서

확인일: 2026-10-22. 로컬 폴더: `C:\Users\User\Desktop\seoul_now_back`.

## 기준 문서

- [백엔드 개발 가이드 — 2·3번 데이터 처리, 1번 공동 영역](https://www.notion.so/3f532d4db2e581468a70fb3faf43a224)
- [사용 데이터 목록 및 검증 현황](https://www.notion.so/3f532d4db2e581b8bd4ed18b52cc543d)
- [개발·운영 공통 가이드](https://www.notion.so/3f532d4db2e58132ab46e4713785d0e2)
- [기술 스택](https://www.notion.so/3f532d4db2e581279e9cebc7fe023138)

노션의 실제 테이블/필드/경로/알고리즘은 담당자가 설계한다는 기준 아래 만든 **주차 부분 초안**이다.
팀이 검토·합의한 전체 DB 설계나 데이터 처리 전체 완료를 뜻하지 않는다.

## 담당 범위와 앞으로의 수정 원칙

사용자 전용 업무는 교통·인구·주차 수집/정규화/적재, 수집 이력·최신성·누락·실패 상태,
도로/주차/행정동 연결 및 지도·추천에 제공할 조회 자료다.
노션은 API·DB·외부 연동을 백엔드와 공동 영역으로 정했다. 이번 OA-21285 조회 API 및 개발 점검 화면 접점 수정은 사용자에게 변경 범위와 이유를 설명하고 승인을 받아 진행했다.

2026-10-22 추가 작업에서는 OA-21285 원천 응답 전체를 저장하는 수집·파이프라인·API·점검 화면을 연결하고 실제 유동인구 및 15개 응답 섹션을 확인했다.
AI 추천·Bedrock·카카오 장소/경로·React 지도·AWS 배포 기능은 구현하지 않았다.
데이터 점검 HTML은 추가했지만 제품 프론트엔드가 아니다.

## 노션 요구사항별 상태

| 요구사항 | 구현 상태 | 남은 일 |
|---|---|---|
| API·파일 수집 | GetParkInfo 전체 페이지 수집과 OA-21285 PRK_STTS 주요장소 단위 수집 구현. 2026-10-22 광화문·덕수궁 실제 API 호출 29건 적재 확인 | 다른 주요장소 범위·호출 제한 검증, 정기 실행 |
| 정적/동적 갱신 분리 | 주차 등록 기본정보의 단발 실행 구현 | 교통·인구 동적 수집, 출처별 주기와 자동 실행 |
| 정규화·적재 | 주차 필수항목·좌표 확인, 출처·시각·원본 선택 필드 보존, DB 갱신 구현 | 타 출처 단위·좌표계·코드 정리 |
| 도로선·소통 연결 | 미구현 | TOPIS 링크 ID·도로선·소통 연결 |
| 행정동 공간 연결 | 미구현 | 버전 확인된 경계와 도로 교차/주차점 포함 관계 |
| 후보 동용 자료 | 미구현 | 백엔드/추천 담당과 지표 합의, 동별 관측 범위 제공 |
| 주변 주차 조회 | 300→500→1000m 첫 유효 반경 조회 구현. GetParkInfo 등록정보와 OA-21285 PRK_STTS 출처를 API·점검 화면에서 선택 가능 | 실시간 제공 표식 Y 및 원천 갱신 15분 이내일 때만 면수 표시. 15분은 화면 정책이며 공식 데이터 갱신 보증이 아님 |
| 인구·센서 지도 자료 | 미구현 | citydata_ppltn·실제 121영역·S-DoT 설치 위치 연결 |
| 최신성·실패 관리 | 주차 원천/수집 시각, 성공/부분/실패 기록, 실패 시 마지막 정상 회차 유지 | 지표별 공개 지연 검증·누락 정책 확장 |
| 중복 회차·늦은 쓰기 | 출처별 advisory lock, 이전 수집 시각의 갱신 방지 구현 | 중단된 running 이력 운영 처리 |
| 지도 조회 자료 | 주변 주차 좌표·기본정보·상태 반환 | 범위/확대 수준별 GeoJSON, 도로선·인구 영역 |
| 출처 목록 관리 | 기존 노션 읽고 로컬 실행 안내 작성 | 실제 호출 후 인증·쿼터·검증 결과 갱신 |

노션상 인구는 지도·별도 현황에만 사용하며 추천 선정·순위·이유에는 사용하지 않는다.
도로에는 ITS 대신 TOPIS를 사용한다. 빈 주차면 수는 현재 구현에서 안내하지 않는다.

## 실제 DB 구축 현황

DB 엔진: PostgreSQL/PostGIS. 기존 연결/공통 Base를 재사용했다.
Alembic revision은 `0002`까지 적용되었다. 기존 `0001`은 PostGIS 확장 활성화다.

### parking_records — 출처별 주차 원천 기록

| 컬럼 | 형식 | 용도 |
|---|---|---|
| id | varchar(64), PK | 출처·코드·이름·주소·좌표의 SHA-256 지문 |
| source | varchar(80), 인덱스 | 현재 적재 출처: seoul_getparkinfo, seoul_citydata_prk |
| source_id | varchar(120) | 원본 PKLT_CD. 단독 고유 키로 사용하지 않음 |
| name | text | 원본 장소명 |
| address | text, nullable | 원본 주소 |
| location | geography(Point,4326), GiST 인덱스 | 위경도 지점과 미터 단위 거리 조회 |
| details | JSONB | 제공된 등록 요금·시간·면수·전화번호 등 선택 필드 원본값 |
| source_updated_at | timestamptz, nullable | 원본 LAST_DATA_SYNC_TM 파싱값. 측정 시각을 새로 만들지 않음 |
| collected_at | timestamptz | 이 회차 수집 시작 시각 |

같은 코드여도 좌표/구획 등이 다르면 별도 기록으로 유지한다. 서로 다른 출처를 실제 동일 시설로
합치지 않았으며 주차면수도 합산하지 않는다. 완전히 동일한 **정규화 결과**만 중복 제거한다.
details는 원본 전체 응답의 보관소가 아니라 선택한 기본정보 필드다. 원본 파일 아카이브는 미구현이다.

### collection_runs — 수집 회차 기록

| 컬럼 | 형식 | 용도 |
|---|---|---|
| id | UUID, PK | 회차 식별 |
| source | varchar(80), 인덱스 | 출처 |
| started_at / finished_at | timestamptz | 시작/종료. 실행 중 종료 시각 null |
| status | varchar(24) | running / success / partial / failed |
| received / accepted / rejected / duplicates | integer | 수신/유효/거절/중복 건수 |
| error_code | varchar(80), nullable | 키·URL을 포함하지 않는 오류 코드 |
| rejection_reasons | JSONB | 좌표 누락 등 제외 이유별 건수 |

실제 확인 결과(2026-10-21): GetParkInfo에서 2,188건을 받아 좌표가 유효한 1,456건을 저장했다. 좌표가 없는 732건은 제외되어 상태는 partial이다. 2026-10-22: OA-21285 광화문·덕수궁 PRK_STTS 29건을 실제 호출·저장했고, 그중 CUR_PRK_YN=Y 1건(세종로 공영주차장, 제공 면수 162, 원천시각 2026-10-22 00:29:23), N 28건을 확인했다. 빈 값/실시간 미제공을 0면으로 바꾸지 않는다. 데모 DB 기록은 제거했다. .env의 키는 로컬에만 보관한다.

## 이미 수정·추가한 파일 전체 목록

### 데이터 처리 전용 파일

추가:
- `app/modules/data_pipeline/collectors/parking.py` — 전체 페이지 수집
- `app/modules/data_pipeline/collectors/citydata_parking.py` — OA-21285 단일 핫스팟의 PRK_STTS 수집
- `app/modules/data_pipeline/normalizers/parking.py` — 주차 변환·검증
- `app/modules/data_pipeline/pipelines/parking.py` — 회차·잠금·적재·실패 처리
- `app/modules/data_pipeline/inspection.html` — 개발용 점검 화면
- `jobs/collect_parking.py` — 등록정보 단발 수집
- `jobs/collect_citydata_parking.py` — 실시간 상태 단일 장소 수집 명령
- `tests/unit/data_pipeline/test_parking.py` — 등록정보 수집/정규화 시험
- `tests/unit/data_pipeline/test_citydata_parking.py` — Citydata 파싱/정규화 시험
- `DATA_PIPELINE_GUIDE.md` — 실행·설정·운영 안내

수정:
- `app/modules/data_pipeline/README.md`
- `jobs/README.md`

### 공동 영역/접점에 이미 반영된 파일

추가:
- `app/clients/seoul.py` — 서울 공공 API HTTP 통신
- `app/models/parking.py` — 주차·수집 이력 테이블 모델
- `app/repositories/parking.py` — 저장·공간 조회
- `migrations/versions/0002_parking_pipeline.py` — 테이블 추가
- `app/api/routes/parking.py` — 조회 API·점검 화면 경로
- `tests/api/test_parking_api.py` — 조회 API 검증
- `tests/integration/test_parking_pipeline.py` — 실제 DB 통합 검증

수정:
- `app/api/router.py` — 주차 경로 등록
- `app/core/config.py` — 인증키 설정 항목 추가
- `app/models/__init__.py` — Alembic 모델 등록
- `compose.yaml` — APP_SEOUL_API_KEY 컨테이너 전달 **한 줄 추가**
- `.env.example` — 키 설정 예시 추가
- `app/clients/README.md` — 실제 연결 코드 안내 갱신
- `README.md` — 주차 추가 상태와 안내 링크 갱신

이번 OA-21285 연동은 데이터 수집기·실행 명령·정규화·환경변수 설정을 추가했다. 로컬 변경이며 Git 커밋·원격 푸시는 하지 않았다.
현재 변경은 로컬이며 Git 커밋·원격 푸시는 하지 않았다.
기존 수정 파일 원본은 작업용 공간 `C:\Users\User\Documents\ChatGPT\서울지도\pipeline-original-backup`에 보관했다.

공동 변경의 이유: 모델/마이그레이션 없이는 DB 저장 불가, 설정 전달 없이는 컨테이너에서
인증키 사용 불가, API 등록 없이는 브라우저 조회 불가. 수집 자체에는 제품 프론트나 AI 변경이 필요 없다.

## 사용 방법과 실제 데이터 연결

1. VS Code에서 seoul_now_back 폴더를 연다. Docker Desktop은 실행 상태로 둔다.
2. <http://localhost:8000/api/v1/parking/inspect>에서 현재 가상 자료와 이력 확인.
3. 실제 키는 프로젝트 루트 `.env`의 `APP_SEOUL_API_KEY=...`(GetParkInfo), `APP_SEOUL_CITYDATA_API_KEY=...`(OA-21285)에 각각 저장한다.
   `.env.example`을 복사할 때 기존 .env가 있으면 덮어쓰지 않는다.
4. 실제 단발 수집 실행:

```powershell
docker compose run --rm --no-deps api python -m jobs.collect_parking
```

5. 점검 화면에서 ‘실제 서울 공영주차장’ 선택 후 조회. 위치에 따라 주변 기록이 없을 수 있다.
6. <http://localhost:8000/api/v1/parking/runs>에서 실행 결과 확인.

키를 넣는 것과 연결 성공은 다르다. 실제 응답·전량 건수·좌표·시각·호출 제한 확인 후에만
실제 API 연동 검증 완료라고 기록한다. 현재는 모의 응답+실제 로컬 DB로 32개 테스트와 코드 검사 통과.

가상 시연 데이터는 점검 화면, 수집 명령, 로컬 DB에서 제거했다. 현재 실제 출처는 seoul_getparkinfo와 seoul_citydata_prk다. Citydata 주변 조회에서 2026-10-22 00:40 KST 기준 세종로 공영주차장(시)의 원천 기준 시각 00:29:23, 주차 가능 162면을 API와 점검 화면에서 확인했다. 화면 정책상 Y 표식과 15분 이내 시각을 만족하는 기록만 면수를 반환하며, 검색 결과 상단에 실시간 제공 행을 우선 노출한다.
명령 종료 코드: 정상0 / 실패1 / 일부 행 제외2 / 이미 실행중3 / 사용자중단130.
현재 자동 반복 실행·AWS 실행은 없다.

## 앞으로 필요한 사용자 제공 정보

- 서울 열린데이터광장 키 발급/승인 여부. 키 값은 채팅 대신 .env에 입력하고 입력 완료만 알린다.
- 데이터별 사용 승인/키 구분, 계정에 표시된 호출 제한(키 값 제외).
- 기존 검증에 쓴 TOPIS 도로 목록·보간점, 121장소 경계, S-DoT 센서 위치,
  전국주차장 파일·행정동 경계의 실제 파일 또는 보관 경로.
- 백엔드 담당과 DB/조회 계약을 맞출 때 공동 파일 변경 승인.

## 지도 선택과 담당 경계

노션 기술 스택은 Kakao Maps SDK, 장소 검색은 Kakao Local, 자동차 ETA는 카카오모빌리티다.
카카오톡 메시지 API가 아니라 카카오맵 지도다. 현 기획을 유지한다면 카카오 구성이 적합하다.
지도 SDK 적용은 프론트 담당이며 데이터 처리용 서울 키와 지도용 카카오 JavaScript 키는 별개다.
데이터 처리 파이프라인은 지도 SDK를 선택하거나 설치하지 않아도 개발·시험 가능하다.
카카오 장소/경로 연결은 공동 영역이므로 승인 없이 추가하지 않는다.

## 후속 작업 순서

1. 실제 GetParkInfo 수집/정규화/저장 검증.
2. 도시 PRK_STTS·전국주차장 파일 기본정보 추가, 출처 중복 검증.
3. 보관 도로선·링크 목록을 이용한 TOPIS 소통 수집 및 연결.
4. 인구·센서 관측 자료 및 공식 관측 영역/설치 위치 연결.
5. 운영용 행정동 버전 확인과 도로 교차·주차점 포함 연결.
6. 백엔드에 지도/추천용 조회 자료 인계. 공동 API 변경은 먼저 승인 요청.
7. AWS 담당에게 단발 명령·종료 코드·주기·관측량·오류 정책 전달.

팀 합의가 필요한 초기값: 주차 수집 지연 7일, 좌표 검증용 넓은 서울 bbox, 회차 부분 적재 정책,
식별 지문과 보관 기간. 7일/bbox는 노션의 고정값이 아니며 행정동 경계 검증을 대신하지 않는다.

## 2026-10-22 추가 — OA-21285 전체 도시데이터 수집

주차 전용 수집기와 별도로 `jobs.collect_citydata`를 추가했다. 이 작업은 OA-21285에서 장소 1곳을 요청하고, 원천 `CITYDATA` 객체 전체를 JSONB에 보존한다. 유동인구(`LIVE_PPLTN_STTS`), 도로(`ROAD_TRAFFIC_STTS`), 주차(`PRK_STTS`), 지하철·버스와 승하차, 사고·통제, 따릉이, 날씨, 충전소, 행사, 상권, 재난문자·뉴스를 포함하며, 응답에 포함되지 않은 항목은 없는 그대로 둔다. 새 필드도 변환 과정에서 버리지 않는다.

- 실행: `docker compose run --rm --no-deps api python -m jobs.collect_citydata --area "광화문·덕수궁"`
- 조회 화면: <http://localhost:8000/api/v1/citydata/inspect> 또는 기존 주차 점검 페이지
- 목록 API: `GET /api/v1/citydata/areas`
- 장소 전체 응답: `GET /api/v1/citydata/areas/{area_code}`
- 신규 DB: `citydata_snapshots` — 장소 코드별 최신 원본 응답 1개. 수집 실행 이력은 `collection_runs`에서 `seoul_citydata`로 확인.
- DB 구조: Alembic `0003` 적용. 기존 스냅샷 데이터는 이미 마이그레이션한 로컬 DB에 보존.
- 실제 확인: 광화문·덕수궁 `POI009` 수집이 success로 완료되어 전체 응답이 로컬 DB에 저장됨.

이 API는 장소당 요청 1회이므로 모든 121개 장소가 자동 수집되는 것은 아니다. 필요한 장소를 골라 각 장소를 수집해야 하며, 자동 주기는 팀이 호출 한도와 신선도 기준을 합의한 뒤 AWS 담당과 연결한다. 이 작업은 지도·제품 프론트 화면이나 AI 추천 모듈을 바꾸지 않았다. 개발용 점검 화면에서 전체 섹션과 원본 JSON을 볼 수 있다.

추가 파일: `collectors/citydata.py`, `pipelines/citydata.py`, `repositories/citydata.py`, `models/citydata.py`, `jobs/collect_citydata.py`, `migrations/versions/0003_citydata_snapshots.py`, `api/routes/citydata.py`, `tests/unit/data_pipeline/test_citydata.py`, `tests/api/test_citydata_api.py`.


2026-10-22 후속: 공식 121장소 XLSX 목록 기반 전체 수집 작업 jobs/collect_citydata_all.py와 로컬 수집 전용 compose.citydata.yaml 추가. 주기적 전체 수집과 점검 화면 60초 자동 새로고침 연결. 기존 단일 장소만 수집한다는 설명은 단일 작업에만 해당하며, 전체 자동 실행은 jobs/README.md 참조.

2026-10-22 시영주차장·차량교통 후속 구현: TRAFFIC_PARKING_STATUS.md 참조. 시영주차장 122건 원본 보존 및 좌표 연결 53건, 도시데이터 주차 1,592건 주변 조회 연동, 주요장소 도로선 4,316구간 GeoJSON, TOPIS 단발 XML 수집 추가. 최신 노션 원문은 접근되지 않아 로컬 정리 문서 기준으로 작업. 이전 미구현 표는 이 후속 상태를 반영하지 않을 수 있음.

2026-10-22 도로 ID 연결 후속: 공식 OA-15060 서비스 도로선과 OA-15061 표준링크 매핑 자료 적재, 유효 선 5,092개. 도시데이터 표준링크 2,569개→서비스링크 1,477개 연결. 나머지 미매핑 ID는 추정하지 않고 보고서에 기록. 광화문 주변 20링크 실제 TOPIS 수집 성공. 전체 교통망 반복 수집은 아직 등록하지 않음. API·실행·프론트 전달 계약 및 47개 테스트 결과는 TRAFFIC_MAPPING_HANDOFF.md 참조.

TOPIS 전체 수집 활성화: compose.citydata.yaml의 topis-collector는 --all --interval 300으로 실행한다. 공식 검증 도로선 5,092개 전체 대상. 광화문 지역 제한 해제. 전체 조회에서는 area_code 파라미터를 제거한다. 진행률 API /api/v1/traffic/collection-status 추가. 상세 상태와 실제 첫 회차 결과는 TRAFFIC_MAPPING_HANDOFF.md 참조.
