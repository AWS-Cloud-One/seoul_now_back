# R01 API 연결 작업

`POST /api/v1/recommendations`의 요청·응답 경계와 Service 연결을 구현했다.
부분 초안 기준이며, 실제 추천 기능 완성이나 팀의 인터페이스 합의를 의미하지 않는다.

## 현재 동작

- UUID·필수 필드·자료형·enum·기본 범위를 요청 모델에서 검사한다.
- 명시적 null과 patch 필드 생략을 구분한다. 출발지·목적의 null 해제는 거부한다.
- request_id, trip_id, base_condition_version은 요청값을 그대로 응답한다.
- JSON 구문 오류는 400, 요청 모델 오류는 422다.
- 기본 상태에서는 검증·추천 어댑터가 없어 503 SERVICE_UNAVAILABLE이다.
  data.kind=failure, condition_update.applied=false이며 기존 조건과 버전을 유지한다.
- 자연어 해석, 외부 API 호출, 자동 재시도, 가짜 후보 반환은 하지 않는다.
- 실행 결과는 no-store이며, 한도 어댑터가 대기 시간을 제공하면 Retry-After도 보낸다.

## 담당자와 맞출 연결 지점

`app/services/recommendations.py`의 두 Protocol은 연결을 위한 제안 인터페이스다.
기존 담당 모듈에 맞는 어댑터를 작성해 get_recommendation_service 의존성에 연결한다.

1. ConditionsValidator.validate(request) → ValidationDecision
   - 현재 조건 재검증, patch 병합, 중복 목적·연령 검사, 필수 추천 조건 검사.
   - 좌표 기준 지원 지역 판별. 클라이언트의 supported=true를 신뢰하지 않는다.
   - 실제 변경 내역·조건 버전·추가 질문을 반환한다. API Service는 이를 임의로 계산하지 않는다.
2. RecommendationEngine.recommend(request, conditions) → RecommendationData
   - 후보 탐색·제외 처리·경로·순위·설명은 추천 담당 모듈에서 수행한다.
   - 요청 여행/버전과 검증 단계의 condition_update를 그대로 반환한다.
   - 정상 탐색의 후보 없음은 no_candidates이며, 장애는 RecommendationFailure로 전달한다.
   - RecommendationFailure(status, code, message, retry_after_seconds)는 사용자에게
     공개할 수 있는 오류만 담는다. 수락 이후 실패에서도 적용 조건은 유지된다.

최종 카드 내부와 context_token 방식은 미확정이므로 result는 임시 JSON 객체다.
result 유무·1~3개 카드 제한만 API 경계에서 확인하며 최종 카드 계약 확정 후 모델을
교체한다. 현재 이 구조로 실데이터 추천이 검증됐다고 해석하면 안 된다.

POST 중복 실행 방지 저장소·유효기간, 본문 크기·제외 ID 개수 상한, 제공처 시간 제한은
미정이다. 새 유료 기능을 실제 연결하기 전에 중복 실행 방지를 함께 구현해야 한다.
현재 409/413 응답은 OpenAPI에 문서화했지만 해당 운영 기능은 구현하지 않았다.
R01은 DB 테이블과 마이그레이션을 추가하지 않는다.
