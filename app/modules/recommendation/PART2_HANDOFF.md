# 파트 2 — 추천 엔진·재추천 인계

## 구현 범위와 정책

분담 문서의 2-1~2-7에 해당하는 내부 기능만 구현했다. 파트 1 조회·SQL·클라이언트·DB·마이그레이션과 파트 3 채팅·문맥 인증·라우터·의존성 조립·프론트는 수정하지 않았다.

분담 문서의 1분/주차거리 정렬보다 이후 사용자 지시를 우선하여 **5분 구간, 확인된 혼잡도, 직선거리 500m 이내 등록 주차장 한 단계 보정, 실제 ETA, 장소 ID** 순을 유지했다. 제한 검사에는 반올림 없는 실제 초를 사용한다. 빈자리·인구·확인되지 않은 분위기/수용성 점수를 사용하지 않는다.

## 구현 완료 항목

- [x] 조건 누락 유지·명시적 null 해제·실제 변경 시 버전 증가·같은 여행의 이전 버전 거부.
- [x] 목적/선정 지역별 다음 페이지와 미노출 후보 저장. 미노출 경로·주차 재조회 후 부족하면 다음 페이지.
- [x] 조건 변경 시 탐색 초기화, 성공 추천/명시적 제외 이력 유지. 일반 후보는 성공 이력에 미포함.
- [x] 같은 여행/요청 ID의 동시 실행 직렬화, 같은 응답 재사용, 내용 충돌, 실패 결과 재사용.
- [x] 원래 후보 경로 재평가 내부 포트. 새 장소 검색·자연어 해석 없음.
- [x] 제공받은 경계·도로·주차 사실을 사용하는 후보 동 선택 정책과 경계 필터.
- [x] 당시 조건·장소·경로·주차·도로 근거·자료 시각·탐색 범위 스냅샷.

## 2-2 후보 동 선정: 제안 수치와 연결 포트

`DestinationRecommendationEngine(..., area_provider=provider)`로 연결한다. provider는 `async areas(origin: Coordinates) -> list[SearchArea]`를 구현한다. 이 파일에서 실제 DB 조회나 HTTP 요청을 추가하지 않았다.

SearchArea 필수 입력: area_id(행정동 식별자), name, center, rings(경도/위도 외곽선과 선택적 내부 구멍), source, geometry_version. 선택 입력: congestion(원활/서행/정체/확인 불가), road_observed_at, registered_parking_count, parking_checked_at.

파트 1은 검증한 서울 행정동 경계와 원천 자료만 제공한다. 행정동 코드와 도시데이터 관측 구역 코드를 혼용하지 않는다. 현재 내부 계약은 단일 Polygon과 내부 구멍을 지원한다. MultiPolygon 자료의 전달 방식은 연결 시 협의해야 한다.

**제안 기본값**: 최대 12개 지역(RecommendationPolicy.max_search_areas), 자료 유효시간 30분, 원천 시각 누락·미래·30분 초과는 미확인. 선택 순서는 확인된 도로 등급 → 유효 등록 주차 기록 존재 → 출발지와 중심점의 근사거리 → 지역 ID다. 이 수치는 확정 기획값이 아닌 조정 가능한 초기 정책이다. 0건은 실제 0, null은 미확인이다. 빈자리를 추정하지 않는다.

검색은 선택 지역 중심점과 기존 카카오 검색 서비스를 사용하고, 결과 좌표를 Polygon 및 서울 지역 조건으로 필터링한다. 지역별 카카오 검색 페이지가 끝나면 다음 지역을 탐색한다. 최대 지역 수/제공처 페이지 한도/이번 요청 예산에 도달한 것은 서울 전체 후보 소진이 아니다.

현재 기본 의존성에는 area_provider가 없으므로 출발지 중심 탐색과 SEARCH_AREA_DATA_INCOMPLETE 경고를 사용한다. 실제 지역 자료 연결은 파트 1·3 담당이다. 지역의 등급을 개별 장소의 도착지 주변 도로 등급으로 복사하지 않는다.

개별 장소의 도로 등급은 Candidate의 congestion/congestion_observed_at/congestion_source로 전달할 수 있다. 원천 시각·출처가 없거나 오래된 경우 엔진은 확인 불가로 처리한다. 현재 공통 어댑터는 이 값을 제공하지 않는다. 실제 조회 연동 없이 혼잡도 반영이 완료됐다고 안내하면 안 된다.

## R01 첫 추천과 추가 추천

공개 R01 요청/응답 스키마는 변경하지 않았다. 예시:

```json
{
  "request_id": "ed3ffea6-6d7d-48a4-9756-0a870043c30c",
  "trip_id": "7a61bd5f-bcd2-49be-9bda-11bdf67625ba",
  "condition_version": 0,
  "current_conditions": {
    "origin": {"label": "시청역", "coordinates": {"latitude": 37.5657, "longitude": 126.977}, "address": null, "region": "seoul", "supported": true},
    "purposes": [{"purpose": "food", "keyword": "한식"}],
    "group_type": "family", "age_groups": null, "headcount": null,
    "max_travel_seconds": 1800
  },
  "conditions_patch": {},
  "excluded_place_ids": []
}
```

추가 추천: 새 request_id, 같은 trip_id, 직전 condition_update.conditions/version을 전달하고 patch는 빈 객체로 둔다. 공개 state 필드는 새로 만들지 않았다. 서버가 여행 전체 성공 추천 이력을 보관하므로 화면 카드 정리로 제외 이력이 없어지지 않는다. 명시적 제외만 excluded_place_ids에 추가해도 된다.

조건 변경: `conditions_patch={"purposes":[{"purpose":"food","keyword":"파스타"}]}`. 다른 조건은 유지한다. 시간 제한 해제는 `{"max_travel_seconds":null}`이고, 최초 null은 제한 없음이다. 조건 변경 후에도 같은 여행의 제외 이력은 유지된다.

같은 요청 재전송: request_id를 포함해 같은 본문을 보내면 외부 조회·결과 ID 생성 없이 같은 응답을 받는다. 실패도 캐시하므로 실제 재시도에는 새 request_id와 마지막 수락된 조건/버전을 보낸다. 같은 요청 ID에 다른 본문은 REQUEST_ID_CONFLICT다.

## 2-6 동일 후보 경로 재평가 포트

파트 3이 결과 문맥·trip_id·원래 조건·후보 ID/좌표를 인증한 후 호출한다:

```python
outcome = await recommendation_service.retry_routes(request, original_candidates)
```

request는 기존 RecommendationRequest이며 조건 patch가 없어야 한다. original_candidates는 원래 일반 후보에 해당하는 Candidate 1~3개다. 후보 재구성 예시(인증은 반드시 먼저 수행):

```python
original_candidates = [
    Candidate.model_validate({
        "place": card["place"]["value"],
        "collected_at": card["place"]["collected_at"],
    })
    for card in authenticated_original_result["cards"]
]
```

원래 카드의 장소 수집 시각을 재사용하며 재전송 때 새 시각으로 만들지 않는다. 같은 request_id 재전송은 후보 목록/시각도 같아야 한다. 새 후보 검색·조건 해석을 하지 않고 제외되지 않은 원래 후보의 경로만 재조회한다. 전부 실패하면 일반 후보, 성공했지만 모두 제한 초과이면 후보 없음, 일부 성공이면 성공 후보만 추천한다. 성공 이력은 한 번만 반영한다.

R02 공개 엔드포인트·문맥 토큰 검증·과거 조건과 현재 조건의 대조는 구현하지 않았다. 이는 파트 3의 책임이다.

## 결과와 당시 근거

기존 result/cards/Information 구조를 유지한다. 결과에 `ranking_policy`와 첫/추가 추천의 `search_scope`를 추가했다. 공통 result는 확장 가능한 JSON이므로 공통 스키마는 수정하지 않았다.

- ranking_policy: time_bucket_seconds=300, parking_bonus_radius_m=500.
- search_scope: 선택 지역의 경계·출처·자료 시각, fallback, more_available. 후보 없음은 result=null이므로 warnings로 범위 제한을 전달한다.
- 카드 reason.evidence에는 원래 ETA/주차와 도착지 주변 도로 등급·원천 시각을 포함한다. 실제 도로 등급을 주차 보정 등급으로 바꾸지 않는다.
- 모델은 서버가 제공한 문구 키만 선택한다. 근거 변경·재정렬·모델 실패 시 후보를 유지하고 기본 설명을 제공한다.
- 결과 문맥 토큰은 여전히 null이다. 결과 저장/조회/인증은 파트 3에서 붙인다.

## 상태 수명·실패·운영 제한

MemoryProgressStore는 새 DB/Redis 없이 단일 프로세스에서 작동하는 초기 구현이다. 기존 엔진 의존성의 여러 요청 인스턴스가 같은 저장소를 사용한다. 테스트/추후 통합에는 생성자 progress_store로 분리된 저장소를 넣을 수 있다.

- 새 실행 완료 후 30분 유휴 TTL. 같은 요청 재전송만으로 TTL을 늘리지 않는다.
- 만료 시 큰 후보/캐시를 정리하고 여행 ID의 작은 만료 표식을 유지하여 이력을 잃은 여행을 조용히 재시작하지 않는다.
- 프로세스당 여행 ID 최대 1,000개(만료 표식 포함), 여행당 완료 요청 최대 200개. 한도 도달은 명시적 오류이며 오래된 캐시를 몰래 지워 동일 요청을 재실행하지 않는다. 장기 운영에는 외부 상태 저장소가 필요하다.
- 서버 재시작·다중 워커에서는 상태 보존/전역 멱등성을 보장하지 않는다. 재시작 후 새 trip_id로 시작해야 한다. 저장 상태 없이 condition_version>0이면 CONTEXT_EXPIRED지만, version=0의 재시작 이전 여행은 새 여행과 구별할 수 없다.
- 동일 여행 요청은 직렬화한다. 다른 여행은 별도로 실행된다. 조건 변경 요청과 추가 추천의 경쟁 시 오래된 조건 요청을 거부한다.
- 완료된 실패는 캐시한다. 처리 중 강제 종료·취소·프로세스 장애에서 외부 제공처 호출 자체의 exactly-once는 보장하지 않는다.
- 조회 실패로 조건이 이미 수락됐으면 condition_update.applied=true를 유지한다. 조건 검증/버전/중복 ID 충돌은 applied=false다.
- 공통 카카오 transport가 인증 오류를 일반 502로 묶어 전달하므로 추천 adapter에서 그 기존 안전 메시지를 명시적으로 구분한다. 공통 transport는 수정하지 않았다. 파트 1의 타입화된 인증 오류가 제공되면 교체할 연결 지점이다.

## 검증과 남은 연결

모의 장소·경로·주차·AI·지역 자료로 실제 서비스/엔진/R01 연결을 검증한다. 테스트는 다음을 포함한다:

- 미노출 후보 사용, 페이지 4 이후 이어가기, 조건 변경 후 제외 유지, 여행 분리.
- 같은 요청 동시 재전송·내용 충돌·실패 재사용·TTL·조건 버전.
- 실제 초 제한, 5분 경계, 500m 경계, 한 단계 보정, 미확인/오래된 혼잡도.
- 선택 지역 밖 장소 제외, 다음 지역 탐색, 자료 부족 경고.
- 원래 후보만 재시도, 일부/전체 실패, 성공 경로 전부 시간 초과, 당시 응답 복제.

실제 OpenAI·카카오 유료 호출이나 파트 1의 새 조회 기능으로 검증한 결과가 아니다. 실제 DB/외부 API/프론트의 최종 통합은 파트 1·3 연결 후 별도 확인이 필요하다.

### 이번 검증 결과

- 격리된 Compose 테스트 DB에서 전체 회귀: 826 passed (189.59초).
- 이후 요청 fingerprint의 명시적 null 구분을 회귀 테스트로 재현·수정했고, 최종 추천/AI/API 관련 검증: 74 passed.
- 변경 Python 파일 Ruff 검사와 git diff --check 통과.
- 공통 API/DB/조회 및 채팅 조건 해석 파일에 변경 없음. 커밋·push는 수행하지 않음.
