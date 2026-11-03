# 환경별 설정

CloudFormation 파라미터 예제와 환경별 설정 설명을 관리한다.

- [seoul-lab.foundation.parameters.json](seoul-lab.foundation.parameters.json): 서울 리전의 foundation 스택 입력 예제. AZ와 CIDR은 실제 계정에서 확인한 뒤 사용한다. 배포 완료 환경을 뜻하지 않는다.
- 콘솔 업로드에서는 같은 값을 직접 입력하고, CloudShell의 `create-change-set`에서는 이 JSON을 `--parameters file://...`로 전달한다.

비밀값은 저장하지 않고 Secret 참조·환경변수 이름과 주입 방법을 문서화한다. API·수집·마이그레이션이 같은 DB 설정을 사용하도록 연결하고, Bedrock 관련 값은 AI 담당과 합의한다. 로컬 개발 기준은 루트 `.env.example`과 `compose.yaml`이다.
