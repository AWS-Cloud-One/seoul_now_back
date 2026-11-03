# API_SPEC 0.12 구현

첨부 API_SPEC.md의 현재 계약을 기준으로 구현했다. API_DESIGN_NOTES.md의 과거
‘문서만 수정’ 기록은 이번 구현 요청의 제한으로 적용하지 않았다.

## 구현 범위

- 기존 `GET /api/v1/health/live`, `/health/ready` 형식 유지.
- `GET /api/v1/map/layers/{roads|parking|population}/geometry`
- `GET /api/v1/map/layers/{roads|population}/status`
- `GET /api/v1/parking/{parking_id}` (시설 UUID)

새 API에는 request_id, server_time, data, error 봉투를 적용한다. 기존 수집·검사용
경로는 호환성을 유지한다. `/parking/nearby` 등 기존 경로는 새 명세 계약으로
전환한 것이 아니며 기존 실시간 주차 조회도 새 제품 API와 구별해야 한다.

지도 쿼리는 bbox와 양의 level을 받는다. level별 단순화는 아직 적용하지 않는다.
한 요청에 최대 10,000개 기준 자료를 허용하며 초과하면 422 MAP_RANGE_TOO_LARGE다.
준비되지 않은 레이어는 503, 버전 불일치는 409다. 위치와 상태는 같은 공간 선택
규칙을 사용한다. 게시 트랜잭션과 조회의 레이어 행 잠금으로 버전 변경을 직렬화한다.

원천 수신 30분 이내 도로값, 관측 후 60분/120분 인구 최신성 규칙을 적용한다.
정상 누락은 이전값으로 채우지 않으며, 실패 후 보존된 값은 허용 시간 내에만
COLLECTION_FAILED와 함께 반환한다. 센서 좌표·AI·외부 재수집 호출은 없다.

## 실행

Python 3.14와 PostgreSQL/PostGIS 환경에서:

```powershell
python -m pip install -r requirements-dev.txt
alembic upgrade head
python -m jobs.publish_map roads
python -m jobs.publish_map population
python -m jobs.publish_map parking
uvicorn app.main:create_app --factory --reload
```

먼저 기존 수집기와 `jobs.import_topis_geometry`, `jobs.import_place_boundaries`로
원천 자료를 적재한다. publish_map은 저장된 자료만 읽는다. 게시 작업을 수집 완료
후 실행해야 새 상태가 반영된다. API 요청은 게시를 실행하지 않는다.

주차는 종료된 success/partial 실행의 검증 통과 자료를 게시한다. failed/running은
게시 대상으로 사용하지 않는다. partial의 누락만으로 기존 시설을 비활성화하지
않으며, 게시 범위 안내에 partial 실행 수와 거절 건수를 보존한다. 수집 상태를
success로 변경하거나 좌표 검증에서 탈락한 기록을 새로 추가하지 않는다.

게시되지 않은 레이어를 빈 지도 성공으로 반환하지 않는다. 기하·기본 속성·ID가
같으면 상태·요금 변경만으로 버전이 바뀌지 않는다. 주차 원본 fingerprint별로
시설 UUID와 출처 기록 UUID를 별도로 발급·보존하며 출처 간 자동 병합하지 않는다.
현재 게시기는 미검증 시설을 대상으로 한다. 수동으로 시설을 병합해 verified로
전환한 데이터는 별도의 검증된 게시 흐름이 필요하다.

주차의 미확인 요금·운영시간은 null이다. 원천 0요금을 무료로 해석하지 않으며
0000~0000 운영시간을 24시간으로 바꾸지 않는다. 빈자리는 항상 unsupported다.

## 미확정 계약

추천·채팅 R01/C01은 부분 초안이며, L01/L02/R02/P01/P02/P03과 관리자 D01은
보류 상태다. 이번 변경에서 이 경로들의 새 계약·추천 알고리즘·AI 응답·문맥 토큰을
임의로 확정하지 않았다. 기존 `/kakao` 외부 연동 경로와 혼동하지 않아야 한다.
전체 명세 구현 완료가 아닌, 구체화된 지도·주차 계약의 구현이다.

추가 확정이 필요한 항목: 추천 후보·순위 서비스, AI 제공자/해석/설명 연동,
지원 지역의 정확한 공간 검증, 중복 POST 응답 보관 기간·공유 저장소,
결과 문맥 검증 방식, 커서 만료, 관리자 인증, 성능 검증 후 지도 한도 조정.

## 검증

```powershell
python -m pytest tests/api tests/unit
python -m ruff check app/models/map.py app/services/map_contract.py app/api/routes/map_contract.py jobs/publish_map.py migrations/versions/0008_map_contract.py tests/api/test_map_contract.py
```

DB가 필요한 실제 마이그레이션·공간 교차·동시 게시 검증은 PostGIS 환경에서 별도로
수행해야 한다. 단위/API 테스트는 DB 대체 객체를 사용한다.
