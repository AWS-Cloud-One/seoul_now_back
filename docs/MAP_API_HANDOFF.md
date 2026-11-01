# M01/M02 및 공통 응답 보완

## 구현 위치

- `app/api/routes/map_contract.py`: HTTP 입력, Swagger, Service 연결.
- `app/schemas/map.py`: bbox/level과 레이어별 GeoJSON·상태 모델.
- `app/services/map_contract.py`: 준비 상태·버전·표시량·정보 최신성·응답 구성.
- `app/repositories/map.py`: 레이어 공유 잠금, PostGIS 교차 조회, 관측 조회.
- `app/schemas/common.py`: 응답 봉투, 오류 상세, Information 공통 모델.
- `app/api/contract.py`: 새 계약 API들의 공통 오류 처리.

## 호출

`GET /api/v1/map/layers/parking/geometry?bbox=126.7,37.4,127.25,37.75&level=6`

도로·인구도 geometry의 layer를 roads/population으로 바꿔 조회한다. status는
roads/population만 허용한다. M01에서 받은 geometry_version과 동일 bbox/level로:

`GET /api/v1/map/layers/roads/status?bbox=126.95,37.54,127.03,37.59&level=6&geometry_version=받은버전`

저장 자료가 미준비면 503 DATA_UNAVAILABLE, 준비된 레이어의 버전 불일치는 409다.
기존 도로/인구 스냅샷이 있다는 것만으로 새 레이어의 게시 준비 완료를 의미하지 않는다.
이번 작업은 외부 수집·자료 게시·도로/인구 데이터 생성·마이그레이션을 실행하지 않는다.

## 보장과 미정 사항

위치/상태 조회는 동일한 활성 기하의 bbox 교차 규칙을 사용한다. 관측 유무로 ID를
선택하지 않는다. 상태 조회는 ID만 선택하고 GeoJSON을 읽어 재전송하지 않는다.
조회 중 레이어 공유 잠금을 유지하며, 기존 게시 작업의 버전 행 잠금과 직렬화한다.
실제 Polygon/MultiPolygon 및 내부 구멍을 보존하고, 센서 지점은 허용하지 않는다.

기존 10,000개 제한은 임시 운영 가드레일이며 성능 검증된 최종 계약이 아니다.
초과 시 전체 오류로 응답하며 앞 N개를 성공 응답으로 절삭하지 않는다. level은 양의
정수로 검사하지만 level별 단순화·최대 확대 수준은 미정이므로 새로 결정하지 않았다.

동적 항목의 음수·비유한 수치·잘못된 인구 범위·미지원 혼잡 코드는 null 처리한다.
실제 0·is_replacement=null은 유지한다. 도로 30분/인구 60·120분 표시 규칙과
수집 실패 후 이전값 표시 정책을 유지하며, 미래/잘못된 시각은 관측 시각으로 사용하지 않는다.

## 공통 계약

지도·주차 상세·R01에 공통 Error/Information/Envelope 모델과 ContractRoute를 사용한다.
GET 요청 ID는 서버 생성, POST는 유효한 본문 request_id를 성공/오류에서 유지한다.
구문 오류 400, 입력 오류 422, DB 장애 503, 예상하지 못한 오류 500을 봉투로 반환한다.
오류에 원문 예외·접속정보를 넣지 않는다. POST 오류도 no-store이며, 확인된 재시도
시간이 있을 때만 Retry-After와 retry_after_seconds를 함께 반환한다.

헬스체크와 기존 /parking/nearby, /map/traffic 등 레거시 API의 응답 형식은 유지한다.
기존 프론트 계약을 공통 봉투로 전환하는 작업은 팀 합의 후 별도로 진행한다.

## 검증

일반 테스트는 외부 제공처나 실데이터를 호출하지 않는다.

```powershell
docker compose exec -T api python -m pytest tests -q
```

공간 교차·비활성 제외·폴리곤 내부 구멍·기하/상태 ID 연결은 실제 로컬 DB에서
아래 opt-in 테스트로 확인한다. 임시 자료는 모든 테스트의 finally에서 롤백한다.

```powershell
docker compose exec -T -e RUN_MAP_DB_TESTS=1 api python -m pytest tests/test_map_postgis.py -q
```
