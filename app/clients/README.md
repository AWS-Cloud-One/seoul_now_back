# 외부 HTTP 연결

중심 담당: 백엔드·데이터 처리 공동.

Kakao 장소·경로, TOPIS·서울 공공 데이터 등 제공처별 인증·요청·응답 변환을 작성할 위치다. SeoulClient가 GetParkInfo JSON 통신을 구현한다. 카카오·TOPIS·인구 연결은 후속이다. 모델 제공처 연결은 [app/modules/ai/providers](../modules/ai/providers/README.md)에서 관리한다.

- 제공처별 모듈을 만들고 API Service와 수집기에서 공유한다. 같은 API의 인증·재시도 코드를 각 담당 폴더에 복사하지 않는다.
- HTTPX 비동기 클라이언트와 설정을 생성자로 전달받는 방식으로 작성한다. API는 lifespan, 수집은 단발 작업 범위에서 연결을 열고 닫는다.
- 타임아웃·실패 상태·합산 호출 예산은 공통 정책과 맞춘다. 이 폴더에서 FastAPI 응답이나 추천 순위를 결정하지 않는다.
- 응답 예제와 모의 자료는 `tests/fixtures/`에 두고, 기본 검사에서는 실제 제공처를 호출하지 않는다.

수집할 전체 페이지·대상·회차의 진행은 `app/modules/data_pipeline/collectors/`에서 관리한다. 이 폴더는 제공처 통신 규칙에 집중한다.
