# 자연어 장소 추천 API

현재 공개 API는 POST /api/v1/recommendations입니다.
GPT가 message를 해석하고 서버가 카카오 후보·경로·순위를 결정한 뒤 GPT가 근거를 설명합니다.

첫 요청:
```json
{"message":"가족끼리 먹을 한식집 추천해줘","conditions":{"x":126.977,"y":37.5657,"max_minutes":30}}
```
추가 요청:
```json
{"message":"다른 가게 추천해줘","state":"직전 응답의 state"}
```
conditions는 query/x/y/max_minutes이며 origin_name/radius_m은 받지 않습니다.
max_minutes 기본값은 30입니다. 시간은 초 원값으로 검사합니다.
카카오 검색은 페이지당 최대 15곳, 미노출 후보 우선 사용, 부족하면 다음 페이지입니다.
순위는 분 단위 내림 ETA → 같은 구간 모두 거리 확인 시 등록 주차 거리 → 장소 ID 문자열순입니다.
혼잡도와 빈자리 수는 순위에 사용하지 않습니다. 가족 적합도 점수는 구현하지 않았습니다.
응답은 recommendations/state/conditions/status이며 장소 링크는 recommendations[*].place.place_url입니다.
경로 실패 후보는 제외하며 전부 일시 실패하면 오류를 반환하는 이전 정책입니다.
조건 변경 시 검색 상태를 초기화하는 이전 정책입니다. state는 30분 만료입니다.

구조화 R01 구현은 내부 참고용으로 보존했으며 현재 공개 라우터에는 연결하지 않습니다.
따라서 API_SPEC의 구조화 요청/nullable 초 제한/general_candidates 계약과는 다릅니다.
채팅 담당자는 기존 AI 모듈과 추천 서비스를 재사용하되 상태·조건 계약을 협의해야 합니다.
OpenAI·카카오 응답은 모의 테스트하며 실제 DB 공간 조회는 로컬 PostGIS로 검증합니다.
