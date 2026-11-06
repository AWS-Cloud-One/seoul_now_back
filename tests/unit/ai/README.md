# AI 단위 테스트

`test_openai.py`는 HTTPX MockTransport로 Responses 요청 형식·strict 스키마·출력 검증·키 미설정·한도·제공처 오류를 검사한다. 실제 모델이나 유료 API를 호출하지 않는다.

조건 해석 후 추천 연결, 추가 질문 시 조건 미적용, 모델 실패·근거 불일치 시 기본 설명 사용은 `tests/api/test_recommendation_integration.py`에서 확인한다. 자연어 해석 품질 자체를 검증한 결과는 아니다.
