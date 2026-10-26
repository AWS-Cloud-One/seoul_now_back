# 초래님 데이터 파이프라인 실행 안내

## 도시데이터 자동 수집 (2026-10-26)

`docker compose up -d`는 `citydata-collector`도 실행합니다. 서울 주요 121개 장소를
한 회차 수집한 뒤 300초 대기하고 반복합니다. 회차 소요 시간은 별도입니다.
`.env`의 `APP_SEOUL_CITYDATA_API_KEY`가 필요합니다. Docker가 실행되어 있어야 하며,
서비스는 `unless-stopped` 정책으로 재시작합니다.
상태 확인: `docker compose logs --tail 10 citydata-collector`.
중지: `docker compose stop citydata-collector`.
서울시 기준 시각과 실제 수집 시각은 다르며, 수집만으로 원천 자료가 최신이 되지는 않습니다.

## 현재 구현한 범위

- 서울 GetParkInfo 공영주차장 기본정보 전량 페이지 수집, 최대 100,000행 상한.
- 공식 신 필드명 PKLT_CD / PKLT_NM / LAT / LOT 사용.
- 네트워크/서버 장애만 최대 3회 시도. 인증·한도·잘못된 요청은 즉시 실패.
- 좌표·필수항목 검증, 같은 코드의 복수 구획 보존, 완전히 동일한 정규화 기록 중복 제거.
- PostgreSQL/PostGIS 적재, 회차 이력, 동일 출처 중복 실행 방지.
- 수집 중 오류는 기존 정상 자료 유지. 한 회차의 DB 저장과 성공 기록은 같은 트랜잭션.
- 300 → 500 → 1,000m 순차 반경 조회, 직선거리·등록 기본정보·원천/수집 시각 반환.
- 가상 시연 데이터는 demo_parking 출처로 분리. 실제 조회에 자동 포함하지 않음.

실제 API 키의 승인/쿼터와 실응답은 아직 확인하지 않았다. 교통·인구·행정동 경계 연결,
다른 주차 출처 병합, AWS 정기 실행, AI·추천은 이 변경의 구현 범위에 포함되지 않는다.
가상 좌표는 기능 시험용이며 실제 시설로 표시하면 안 된다.

## 1. 환경 실행

VS Code 터미널에서 프로젝트 루트로 이동한다.

```powershell
docker compose up -d
docker compose run --rm migrate
```

마이그레이션 0002가 주차 원천 기록과 수집 이력 테이블을 추가한다.
API 프로세스가 직접 테이블을 만들거나 데이터를 수집하지 않는다.

## 2. 키 없이 시연

```powershell
docker compose run --rm --no-deps api python -m jobs.collect_parking --demo
```

가상 자료 3행 중 정상 좌표 2행을 적재하고 누락 1행을 거절한다.
따라서 `status=partial`, `accepted=2`, `rejected=1`, 종료 코드 2가 예상 결과다.
같은 명령을 반복해도 주차 기록은 중복 증가하지 않는다. 실행 이력은 매번 추가된다.

- 실행 이력: <http://localhost:8000/api/v1/parking/runs>
- 가상 자료 조회: <http://localhost:8000/api/v1/parking/nearby?lat=37.5665&lon=126.978&source=demo_parking>
- API 문서: <http://localhost:8000/docs>
- 데이터 점검 화면: <http://localhost:8000/api/v1/parking/inspect> (제품 프론트와 별개의 개발 도구)

## 3. 실제 데이터 수집

`.env.example`을 **기존 .env가 없다면** .env로 복사한다. .env가 이미 있다면 내용을 유지하고
다음 한 줄만 추가/수정한다. 실제 키를 Git이나 채팅에 올리지 않는다.

```text
APP_SEOUL_API_KEY=본인의_서울_열린데이터광장_인증키
```

```powershell
docker compose run --rm --no-deps api python -m jobs.collect_parking
```

실제 응답은 `seoul_getparkinfo`로 적재된다. URL·제공기관 오류 메시지는 키 유출 방지를 위해
로그에 출력하지 않는다. 공식 API 문서의 HTTP 엔드포인트를 사용한다.
실제 호출 전 계정별 이용 조건·쿼터·프로젝트 공동 사용 권한을 확인한다.

- 실제 조회: <http://localhost:8000/api/v1/parking/nearby?lat=37.5665&lon=126.978>
- 원천 문서: <https://data.seoul.go.kr/dataList/openApiView.do?infId=OA-13122&srvType=A>

키 없이 실제 수집을 실행하면 `missing_api_key`, 종료 코드 1을 반환한다.
빈 응답/페이지 누락/변경 중인 전체 건수는 성공으로 덮어쓰지 않는다.

## 회차·재실행·상태 계약

| 종료 코드 | 의미 | AWS 처리 제안 |
|---|---|---|
| 0 | 전부 정상 | 정상 종료 |
| 1 | 수집/정규화/DB 실패 | 이력 확인, 원인별 제한 재시도 |
| 2 | 일부 잘못된 행 제외 후 정상 행 저장 | 부분 성공 알림, 무한 재시도 금지 |
| 3 | 같은 출처가 이미 실행 중 | 겹친 실행 건너뜀 |
| 130 | 사용자 중단 | 재실행 전 이력 확인 |

출처별 PostgreSQL 세션 advisory lock을 사용한다. 외부 수집 동안 적재 트랜잭션은 열지 않는다.
이전 수집 시각보다 오래된 upsert는 새 값을 덮지 않는다. 강제 종료 시 `running` 이력이 남을 수
있으므로 운영에서 작업 종료 상태와 대조해야 한다. 자동 실패 전환·보관 기간 정리는 후속이다.

마지막 성공/부분 성공 회차의 자료만 새 주변 검색 대상으로 사용한다. 실패한 회차는 마지막
정상 자료를 교체하지 않으며 응답에 최신 시도 상태를 함께 제공한다. 오래전 회차의 행을
파괴적으로 삭제하지 않는다. 과거 전체 스냅샷 이력을 재구성하는 시스템은 아니다.
동일 ID의 정상 자료는 최신 값으로 갱신된다.

7일 초과는 **주차 기본정보 수집 지연 표시의 개발 초기값**이다. 원본 등록 정보가 현장 최신임을
의미하지 않는다. 요금 0/공란, 0000~0000 운영시간은 무료/24시간으로 해석하지 않는다.
원천 시각 파싱 불가 시 null과 원본 문자열을 보존한다. 빈자리는 항상 미확인이다.
주차 ID는 출처+코드+이름+주소+좌표 지문이며 실세계 시설을 합치는 근거가 아니다.

## 테스트

```powershell
docker compose run --rm --no-deps api ruff check .
docker compose run --rm --no-deps api ruff format --check .
docker compose --profile test run --rm tests
```

실제 제공기관이나 유료 AI를 호출하지 않는다. 통합 테스트는 자체 test 출처의 임시 기록만
생성하고 정리한다. 기본 PostgreSQL/PostGIS 테스트 및 Alembic 모델 비교도 함께 실행한다.

## 팀과 맞출 사항

- 서영님: 새 테이블·API 필드 및 이미 작업 중인 모델과 충돌 여부 검토.
- 호영님: 위 단발 명령·종료 코드와 스케줄러 연결. 주차 기본정보 주간 변경 점검은 초기안.
- 상혁님: 미확인·등록값·직선거리 의미를 추천에 정확히 전달.
- 다음 구현: 보관된 TOPIS 링크/도로선, 121영역 경계·인구 응답, S-DoT 센서 위치 자료 인계.

원본·정제 파일은 Git에서 제외되어 있어 저장소만 복제하면 기존 검증 데이터가 오지 않는다.
인증키와 파일 위치를 확보한 후 나머지 출처를 같은 구조로 확장한다.
