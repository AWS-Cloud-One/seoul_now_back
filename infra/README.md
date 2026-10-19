# AWS 인프라 작업 공간

중심 담당: AWS. 기획서의 CloudFormation·ECS·RDS·수집 제어·배포 운영 초안을 구체화할 위치다.

**현재는 디렉터리와 안내만 준비했다.** 배포 가능한 템플릿, 실제 AWS 자원·IAM 권한·리전·자원 크기는 아직 정하지 않았다.

| 위치 | 작성할 내용 |
| --- | --- |
| [cloudformation](cloudformation/README.md) | 네트워크·DB·ECS·수집 제어·권한·관측 템플릿 |
| [environments](environments/README.md) | 환경별 비밀이 아닌 파라미터 예제·설정 설명 |
| [runbooks](runbooks/README.md) | 배포·롤백·DB 복구·수집 장애 대응 절차와 검증 기록 |
| [docker.md](docker.md) | 루트 Dockerfile·compose.yaml의 빌드·실행 안내 |
| [../.github/workflows](../.github/workflows/README.md) | GitHub Actions 검사·배포 워크플로 |

현재 백엔드 코드에서 확인할 실행 정보:

- API 앱: `app.main:create_app` factory, 기본 포트 8000. 루트 Dockerfile의 production 단계는 `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000`으로 실행한다. 로컬 Compose는 development 단계와 자동 재시작을 사용한다.
- 헬스체크: `/api/v1/health/live`, `/api/v1/health/ready`.
- DB 변경: 로컬 Compose의 `migrate` 서비스가 `alembic upgrade head`를 실행한다. AWS에서도 같은 이미지의 별도 작업으로 적용하고 성공 후 API를 배포하도록 구성한다.
- 환경변수 기준: 루트 `.env.example` 및 `app/core/config.py`의 `APP_` 설정.
- 수집 명령·회차 결과·종료 코드: `jobs/`에 실제 작업이 추가될 때 데이터 담당과 확정한다. 현재 실행 가능한 수집 작업은 없다.
- 테스트·평가: `tests/README.md`, `evals/README.md`의 범위와 실제 검증 상태를 확인한다.

API와 수집의 실행 단위를 나누고 모델·추천 기능마다 별도 서버를 만들지 않는다. 프론트 배포와 CloudFront 진입점 등 공통 인프라는 프론트 담당과 연결하되 앱 구현은 해당 프론트 저장소에서 관리한다. AWS IAM 권한·Secret 참조와 앱의 실제 환경변수 이름을 함께 맞춘다.
