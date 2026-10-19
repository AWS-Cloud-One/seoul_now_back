# GitHub Actions 작업 공간

중심 담당: AWS, 검사 단계는 백엔드 검증 담당과 공동 구성한다.

현재 활성 워크플로는 없다. 실행 패키지는 루트 `requirements.txt`, 개발 패키지는 `requirements-dev.txt`로 고정하며, Dockerfile에서 설치한다. 검사 명령은 루트 README의 Docker 명령을 기준으로 YAML 워크플로에 연결한다. 구성 순서는 기본 검사 → 이미지 빌드 → 합의된 환경 배포로 구체화한다.

배포 연결은 기획서의 GitHub OIDC 방향을 따르고, 실행 환경·역할·변경 범위는 AWS 담당이 정한다. 실제 외부 API·유료 모델 평가는 기본 검사와 분리한다. 이 README 자체는 워크플로를 실행하지 않는다.
