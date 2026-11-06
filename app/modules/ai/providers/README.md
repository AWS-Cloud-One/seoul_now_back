# 모델 제공처 연결

현재 `openai.py`가 OpenAI Responses API를 HTTPX 비동기 클라이언트로 호출한다. 기본 모델 `gpt-4.1-mini`는 기존 추천 구현의 선택을 유지한다. `APP_OPENAI_API_KEY`, `APP_OPENAI_MODEL`, `APP_OPENAI_TIMEOUT_SECONDS`는 공통 설정에서 읽으며 Compose의 API 서비스에만 전달한다. 새 패키지나 별도 requirements 파일은 필요하지 않다.

API와 분리된 독립 AI 서버를 추가하지 않고, 클라이언트를 생성하여 AI 처리 모듈에 전달한다. 모듈 import나 앱 시작만으로 유료 모델을 호출하지 않는다.

호출에는 `store: false`와 `text.format`의 strict JSON Schema를 사용한다. 완료 여부·거절·출력 구조를 확인한 뒤 Pydantic 타입으로 검증한다. 오류는 `ServiceError`로 변환하고 원문 오류 본문이나 키를 응답에 노출하지 않는다. 429의 대기 시간은 공통 오류로 전달하며 자동 재시도하지 않는다. [공식 Structured Outputs 안내](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)를 기준으로 요청 형식을 맞췄다.

Bedrock은 아직 연결하지 않았다. 도입 시 AWS 리전·모델·IAM 권한을 맞추고 동일한 모듈 경계에서 교체한다. 실제 모델 품질·유료 호출은 모의 응답 테스트와 별도로 검증해야 한다.
