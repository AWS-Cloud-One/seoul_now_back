
## 구현됨 (2026-11-05)

OpenAI Responses API를 기존 HTTPX 비동기 클라이언트로 호출합니다. Bedrock은 사용하지 않습니다.
`providers/openai.py`는 구조화 출력·오류를 검증하고 `interpretation.py`는 명시된 조건만 합칩니다.
`explanation.py`는 모델이 선택한 근거 키와 서버 순서를 검증하고 서버 수치로 설명을 렌더링합니다.
프롬프트는 `prompts/interpretation.txt`, `prompts/explanation.txt`입니다.
설정·실행·모의 검증의 범위는 [추천 안내](../../../docs/RECOMMENDATION_GUIDE.md)를 참고하세요.
아래는 초기 설계 기록으로 '미구현' 및 Bedrock 예정 설명은 이 항목으로 대체합니다.

중심 담당: 백엔드 검증·AI 추천. 전체 작업 위치는 [README의 역할별 작업 위치](../../../README.md#역할별-작업-위치)를 따른다.

이 패키지는 자연어 조건 해석, 구조화 출력 검증, 확정된 추천 근거의 설명을 담당한다. 서버의 후보 비교·최종 순위는 `app/modules/recommendation/`에서 처리한다.

| 위치 | 작성할 내용 |
| --- | --- |
| `providers/` | Bedrock 등 모델 제공처 연결, 호출 제한·오류·사용량 처리 |
| `prompts/` | 조건 해석·설명 프롬프트와 변경 기록 |
| 이 패키지의 기능별 모듈 | 향후 `interpretation.py`, `explanation.py`, `validation.py` 등으로 조건 해석·설명·근거 검사를 구현 |

현재는 패키지와 작업 안내만 있다. 실제 모델 호출, 프롬프트, 입력·출력 계약과 boto3 설치는 미구현이다. 각 계약은 API·추천 담당과 맞춘 뒤 `app/schemas/`에 공통 타입으로 정의한다.

- API Service가 AI 모듈을 호출한다. 이 패키지에서 Router나 `Request`를 사용하지 않는다.
- 조건 해석 실패와 설명 실패의 대체 동작을 구분한다. 실패를 성공한 빈 결과로 바꾸지 않는다.
- 실제 후보 ID·순위·수치와 설명의 일치는 서버에서 검증한다.
- 제공처 클라이언트를 전달받을 수 있게 구현하여 기본 테스트에서는 모의 응답을 사용한다.
- 검증 코드는 [AI 단위 테스트](../../../tests/unit/ai/README.md), 품질 평가 자료는 [evals](../../../evals/README.md)에 둔다.
