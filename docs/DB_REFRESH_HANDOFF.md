# 재부팅 후 작업 재개 — 2026-11-03

> Docker 복구 후 재개했다. 최신 적용·검증 결과는 `docs/DB_REFRESH_VERIFICATION.md`를 참고한다.
> 아래 내용은 재부팅 전 기록이며 당시 head `0006`은 실제 DB 이력과 맞춘 뒤 `0008`로 변경했다.

작업 폴더: `C:\Users\User\Documents\ChatGPT\서울지도\main-backend-release`

브랜치: `codex/db-refresh-integration`. 변경 파일은 저장했으며 커밋·push는 하지 않았다.
작업 시작 전부터 `app/models/__init__.py`, `app/models/external_api.py`,
`migrations/versions/0005_external_api_history.py`, `data/EXTERNAL_API_TABLES.md` 변경이 있었다.
기존 변경을 보존하고 그 위에 수집 이력 연결을 구현했다.

## 완료한 코드

- 도시데이터·주차 최신값과 응답/관측 이력을 같은 트랜잭션으로 저장.
- 제공처 스냅샷 이력과 실패 시도 기록. 실패 시 이전 정상값 보존.
- 별도 scheduler worker, 작업별 DB 잠금, 시간 제한, 실패 후 다음 주기 재시도.
- Compose의 기존 citydata-collector 서비스를 공통 scheduler로 연결.
- 최근 수집 결과 GET `/api/v1/collections/runs`와 프론트 연동 계약.
- Alembic 0006 조회 인덱스와 동시 migration 방지 잠금.
- 운영 DB와 분리된 `compose.validation.yaml` 및 통합 테스트.
- 실행·저장 정책: `docs/DB_REFRESH_RUNBOOK.md`.

## 확인 결과

- Python 3.14.10 검증 환경: `.venv314` (프로젝트 고정 의존성 설치).
- 전체 pytest: **67 passed, 5 skipped**. 건너뛴 5개는 실제 PostGIS DB가 필요한 테스트.
- Ruff 및 `git diff --check` 통과. Compose 두 파일의 config 검증 통과.
- Alembic head는 0006 단일 head. 전체 upgrade SQL 생성 확인.
- 실제 PostgreSQL DB에는 접속하지 못했으며 migration 적용도 하지 않았다.
- Docker Desktop은 CLI 응답 시간 초과, UI도 열리지 않아 사용자가 PC를 재부팅하기로 했다.
- 로컬 설정의 DB는 127.0.0.1:5432/seoul_now. 수집 키는 도시데이터만 설정돼 있다.
  키 값은 출력하거나 문서에 기록하지 않았다.

## 재개 순서

1. Docker 엔진 복구를 `docker version`으로 확인.
2. `docker compose -f compose.validation.yaml run --build --rm tests`로 격리 DB migration·전체 테스트.
3. 테스트 실패 시 수정하고 `alembic check`를 포함해 모델과 실제 스키마 일치 확인.
4. 개발 DB의 기존 revision·테이블을 확인한 다음 `docker compose up --build -d`로 적용 및 실행.
5. 도시데이터 수집 1회와 조회 API 결과, 다음 주기 자동 재실행 로그를 확인.
6. 결과를 runbook에 기록하고 사용자에게 실제 적용 여부를 구분해 보고.

Windows sandbox에서는 asyncio 테스트가 정지했지만 일반 실행 권한에서는 정상 통과했다.
Git 메타데이터는 원본 `C:/Users/User/Desktop/seoul_now_back/.git`을 가리키므로
브랜치 생성에 별도 실행 권한이 필요했다. 작업 파일은 현재 폴더에 있다.

AWS 배포·지도 제공처 도메인 등록·Bedrock 비교는 실행하지 않았다.
추천 모듈은 기존에 구현이 없어 실제 성능·품질 비교 결과를 만들지 않았다.
사용자 재부팅 안내 이후 추가 구현은 중단하고 상태를 저장했다.
