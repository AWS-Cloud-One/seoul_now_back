# AWS 인프라 작업 공간

중심 담당: AWS. EKS의 EC2 관리형 노드 그룹에서 API·수집을 실행하고 RDS·S3·CloudFront에 연결할 작업 공간이다.

**첫 단계인 네트워크·저장소 CloudFormation 템플릿을 준비했다.** [AWS 첫 설정 안내](runbooks/01-foundation.md)에 따라 콘솔에서 파일을 업로드하거나 CloudShell로 적용할 수 있다. 템플릿 작성과 실제 계정 배포는 별개이며, EKS·RDS·앱 배포 템플릿은 후속 작업이다.

| 위치 | 작성할 내용 |
| --- | --- |
| [cloudformation](cloudformation/README.md) | 기본 네트워크·ECR·S3 템플릿. EKS·DB·권한·관측은 후속 |
| [environments](environments/README.md) | 환경별 비밀이 아닌 파라미터 예제·설정 설명 |
| [runbooks](runbooks/README.md) | 배포·롤백·DB 복구·수집 장애 대응 절차와 검증 기록 |
| [docker.md](docker.md) | 루트 Dockerfile·compose.yaml의 빌드·실행 안내 |
| [../.github/workflows](../.github/workflows/README.md) | GitHub Actions 검사·배포 워크플로 |

현재 백엔드 코드에서 확인할 실행 정보:

- API 앱: `app.main:create_app` factory, 기본 포트 8000. 루트 Dockerfile의 production 단계는 `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000`으로 실행한다. 로컬 Compose는 development 단계와 자동 재시작을 사용한다.
- 헬스체크: `/api/v1/health/live`, `/api/v1/health/ready`.
- DB 변경: 로컬 Compose의 `migrate` 서비스가 `alembic upgrade head`를 실행한다. AWS에서도 같은 이미지의 별도 작업으로 적용하고 성공 후 API를 배포하도록 구성한다.
- 환경변수 기준: 루트 `.env.example` 및 `app/core/config.py`의 `APP_` 설정.
- 수집: 현재 `main`의 Compose는 도시데이터 반복 수집과 S-DoT 루프를 실행한다. 배포용 공통 스케줄러 진입점은 작업 브랜치 통합 후 확정한다.
- 테스트·평가: `tests/README.md`, `evals/README.md`의 범위와 실제 검증 상태를 확인한다.

API와 수집의 실행 단위를 나누고 모델·추천 기능마다 별도 서버를 만들지 않는다. 프론트 배포와 CloudFront 진입점 등 공통 인프라는 프론트 담당과 연결하되 앱 구현은 해당 프론트 저장소에서 관리한다. AWS IAM 권한·Secret 참조와 앱의 실제 환경변수 이름을 함께 맞춘다.
