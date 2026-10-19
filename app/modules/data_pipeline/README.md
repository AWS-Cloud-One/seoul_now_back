# 데이터 수집·처리

중심 담당: 데이터 처리. API·DB·공간 조회·외부 연결은 백엔드와 공동으로 관리한다.

```text
jobs의 단발 실행
  → pipelines: 한 회차의 진행·검증·트랜잭션
      → collectors: 필요한 API 페이지·파일 수집
      → normalizers: 좌표·단위·시각·누락 상태 변환
      → app/repositories: 공통 DB에 저장
```

현재는 패키지와 작성 위치만 구성했다. 실제 수집 대상·회차 계약·DB 적재는 아직 구현하지 않았다.

| 위치 | 작성할 내용 |
| --- | --- |
| `collectors/` | 출처별 페이지·대상·파일 수집 흐름, 수집 시각과 원천 자료 전달 |
| `normalizers/` | 제공처 자료의 필드·좌표계·단위·시간 정규화와 유효성 검사 |
| `pipelines/` | 수집 → 정규화 → 적재를 묶는 회차 처리, 실패·중복·늦은 쓰기 처리 |

제공처 HTTP 통신은 [공통 clients](../../clients/README.md)를 사용한다. DB 쿼리는 공통 `app/repositories/`, 테이블은 `app/models/`, 변경 이력은 루트 `migrations/`를 사용한다. 별도 ORM Base·DB 연결 설정·Alembic 이력을 만들지 않는다.

데이터 처리 결과를 API가 읽을 수 있도록 필요한 공간 조회 함수를 공통 Repository에 추가한다. 정기 수집 코드를 API 요청이나 FastAPI lifespan에서 실행하지 않는다. 단발 실행의 설정·세션 생성은 [jobs](../../../jobs/README.md)에서 연결한다.

원본·중간 산출물의 로컬 위치는 [data](../../../data/README.md), 공유할 작은 검증 표본은 [tests/fixtures](../../../tests/fixtures/README.md)다. 데이터 사용 범위·원천 시각·단위가 확인된 후에만 기능에 연결한다.
