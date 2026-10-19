# 컨테이너 이미지

중심 담당: AWS. 실행 이미지 정의는 루트 [Dockerfile](../Dockerfile), 로컬 서비스 구성은 [compose.yaml](../compose.yaml)에 있습니다. 이 문서는 빌드·실행 방법을 설명합니다. 처음 실행하는 팀원은 [루트 README](../README.md)의 `docker compose up --build -d`부터 따라가면 됩니다.

## 이미지 단계

| 빌드 단계 | 포함 내용 | 용도 |
| --- | --- | --- |
| `runtime` | Python·실행 패키지·앱·migration·jobs | 실행 이미지의 공통 기반 |
| `development` | runtime + pytest·Ruff·tests | 로컬 개발, 자동 재시작, 검사 |
| `production` | runtime | 테스트 도구와 자동 재시작을 제외한 배포용 이미지 |

Python 버전과 OS 이미지는 Dockerfile의 첫 `FROM`에서 함께 고정합니다. 실행 패키지는 `requirements.txt`, 개발 패키지는 이를 참조하는 `requirements-dev.txt`에 직접 작성합니다. 빌드 때 `pip install -r`로 설치하고 `pip check`로 의존성 충돌을 확인합니다.

기본 `docker build`는 마지막 단계인 `production`을 만듭니다. Compose는 `development`를 사용합니다.

```bash
# 저장소 루트에서 배포용 이미지 빌드
docker build -t seoul-now-backend:prod .

# 특정 배포 아키텍처로 빌드할 때
docker build --platform linux/amd64 -t seoul-now-backend:prod-amd64 .
```

이미지는 UID 10001인 `app` 사용자로 실행합니다. 기본 명령은 `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000`입니다. DB 주소 등 `APP_` 환경변수는 컨테이너 실행 시 전달합니다. `.env`, 개인 `agent/`·`AGENTS.md`, 가상환경, 로컬 데이터·평가 결과는 이미지에 포함하지 않습니다.

## 로컬 실행 순서

`compose.yaml`은 DB가 healthy가 되면 `migrate`에서 `alembic upgrade head`를 실행하고, 성공 종료한 뒤 API를 시작합니다. API 프로세스 자체에는 migration이나 수집 자동 실행이 없습니다.

- `api`: 소스 폴더를 연결하고 `--reload`로 실행합니다.
- `migrate`: DB 변경을 적용하고 종료합니다. `Exited (0)`이 정상입니다.
- `tests`: `test` 프로필에서만 실행하며 실제 PostGIS를 포함해 검사합니다.

Dockerfile의 기본 헬스체크는 `/api/v1/health/live`입니다. 로컬 Compose는 `/api/v1/health/ready`를 사용해 DB까지 확인합니다. AWS의 생존·준비 상태 판정은 운영 요구에 맞춰 따로 설정합니다.

PostGIS 이미지는 `linux/amd64`로 고정되어 Apple Silicon에서 에뮬레이션을 사용합니다. Python 이미지는 amd64·arm64를 지원합니다. Python·PostGIS의 태그 또는 digest를 변경할 때는 의존성 설치와 DB 통합 검사를 다시 수행하세요.

## AWS 연결 시 남은 작업

배포용 이미지가 AWS 자원까지 구성하지는 않습니다. AWS 담당은 ECR·ECS, 실행 환경변수·Secret, 네트워크·권한, 별도 migration 작업과 배포 순서를 연결해야 합니다. Compose의 DB 계정·포트 설정은 로컬 개발용입니다.

`jobs/`에 수집 진입점이 구현되면 같은 이미지에서 `python -m jobs.<작업명>`으로 별도 실행할 수 있습니다. 아직 실행 가능한 수집 명령은 없습니다.
