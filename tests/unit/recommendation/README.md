# 추천 단위 테스트

`test_components.py`는 분 단위 정렬·주차 미확인 시 비교 제외·장소 ID 사전순, 조건의 생략/해제, 카카오 응답의 필드·수치·오류 변환을 검사한다. 고정된 입력과 HTTPX MockTransport를 사용한다.

실제 서비스·검증기·엔진을 연결한 제외·목적·지역·시간 제한·부분 실패·공통 응답·채팅 검사는 `tests/api/test_recommendation_integration.py`에 있다. DB나 실제 외부 API를 호출하지 않는다.
