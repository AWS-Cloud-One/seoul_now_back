# 모델 제공처 연결

2026-11-05: 사용자 선택에 따라 Bedrock 대신 `openai.py`에 OpenAI Responses API를 구현했습니다.
설정은 APP_OPENAI_API_KEY, APP_OPENAI_MODEL, APP_OPENAI_TIMEOUT_SECONDS입니다.
기존 HTTPX를 사용하므로 boto3/OpenAI SDK 의존성을 추가하지 않습니다.
실제 키가 없는 상태의 호출은 503을 반환합니다. 아래 Bedrock 안내는 이전 계획입니다.

AI 담당이 Bedrock Converse 연결을 구현할 위치다. 아직 구현된 제공처는 없다.

API와 분리된 독립 AI 서버를 추가하지 않고, 클라이언트를 생성하여 AI 처리 모듈에 전달한다. 모듈 import나 앱 시작만으로 유료 모델을 호출하지 않는다.

실제 구현 시 AWS 담당과 리전·모델/프로파일·IAM 권한·환경변수 이름을 맞춘다. boto3의 동기 호출을 FastAPI 이벤트 루프에서 직접 실행하지 않도록 실행 위치·동시성·시간 제한을 함께 정한다. SDK 버전은 실제 도입 시 호환성을 확인하고 루트 `requirements.txt`에 버전을 지정해 추가한 뒤 Docker 이미지를 다시 빌드한다. 빌드 명령은 루트 README를 따른다.
