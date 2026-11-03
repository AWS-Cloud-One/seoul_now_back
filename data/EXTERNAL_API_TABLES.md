# 외부 API 수집 저장 테이블

2026-10-27 추가. 기존 최신값 테이블은 유지하며 이력용 테이블 2개를 추가한다.

## 관계

`collection_runs` 1 → N `external_api_responses` 1 → N `external_api_observations`

- collection_runs: 기존 회차 상태·시작/종료·수신/정상/거절 건수·오류 코드.
- external_api_responses: 호출 또는 페이지 단위 응답 이력. source는 기존 수집기와
  같은 출처 코드, operation은 키 없는 기능 이름, collected_at은 실제 수집 시각이다.
  payload는 인증값을 제거한 파싱 응답(JSON 객체/배열)이다.
- external_api_observations: 응답에서 추출한 장소/센서/카메라별 자료. record_key는
  원천 식별자이며 동일 ID의 하위 자료가 있으면 구분자를 포함한다. observed_at은
  원천 관측 시각이며 제공되지 않으면 NULL이다. 좌표는 WGS84 위도/경도이며
  둘 다 있거나 둘 다 NULL이어야 한다. data에는 측정 항목과 단위를 명확히 보존한다.

## 사용 원칙

1. 회차를 만든 뒤 응답 및 그에 속한 관측값을 같은 DB 트랜잭션으로 저장한다.
2. UUID는 애플리케이션에서 생성한다. 같은 응답 안의 record_key는 중복 저장할 수 없다.
3. API 키·인증 헤더·키 포함 요청 URL/영상 URL은 저장하지 않는다. 필요한 영상 주소는
   인증값을 제외한 식별 정보로 관리하고 실제 조회 시 제공처 정책에 맞게 처리한다.
4. CCTV 자료는 카메라 정보이며 인구 측정값이 아니다. 영상 파일은 이 테이블에 넣지 않는다.
5. 최신값 테이블 provider_snapshots/citydata_snapshots/parking_records는 기존 조회에 사용한다.
6. 도시데이터·주차·제공처 스냅샷 수집기의 이력 저장이 연결되었다. 도시데이터와 기타
   제공처는 전체 스냅샷 단위, 주차는 정규화 기록 단위로 관측값을 저장한다. 보관 기간·정리
   정책과 UTIC 호출 코드는 별도 구현이 필요하다. 상세 절차는 [운영 안내](../docs/DB_REFRESH_RUNBOOK.md)를 따른다.

적용: `docker compose exec api alembic upgrade head`

조회 예시:

```sql
SELECT r.source, o.record_key, o.name, o.observed_at, r.collected_at, o.data
FROM external_api_observations o
JOIN external_api_responses r ON r.id = o.response_id
WHERE r.source = 'seoul_citydata'
ORDER BY r.collected_at DESC
LIMIT 100;
```
