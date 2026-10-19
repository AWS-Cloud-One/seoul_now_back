# 로컬 데이터 작업 공간

데이터 처리 담당의 원본·중간 산출물 위치다. 프로그램 코드는 `app/modules/data_pipeline/`, 단발 실행 진입점은 `jobs/`에 둔다.

- [raw](raw/README.md): 내려받은 원본 API 응답·파일.
- [processed](processed/README.md): 정규화·변환·검증 중간 결과.

두 디렉터리는 README를 제외하고 Git에서 제외한다. 팀과 공유할 작은 검증 표본은 `tests/fixtures/`에 출처·시각·가공 내용을 기록해서 추가한다. 원본의 보관 위치와 다운로드·변환 절차는 담당 문서로 공유한다.
