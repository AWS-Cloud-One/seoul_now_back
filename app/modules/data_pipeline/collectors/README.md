# 수집기 작성 위치

교통·인구·주차의 제공처별 수집 코드를 둔다. 현재 `citydata.py`는 도시데이터 요청과 공식 장소 목록 파일 읽기, `parking.py`는 주차 전체 페이지 수집, `topis.py`는 도로별 교통 요청을 담당한다. 파일 경로는 호출자가 전달한다.

`app/clients/`의 통신 기능으로 필요한 전체 페이지·대상을 조회하고 원천 자료·출처·수집 시각·누락을 전달한다. 정규화와 DB 적재는 각각 인접한 `normalizers/`, `pipelines/`로 분리한다. 직접 스케줄러를 시작하지 않는다.

`supplemental.py`는 SGIS·S-DoT·ITS CCTV·전국 주차 API와 S-DoT 설치 목록 파일을 읽는다. 전국 주차 수집은 `collect_national_parking()`이며, `parking.py`의 서울 주차 수집 `collect_parking()`과 구분한다. DB 세션을 받거나 공간 조회·저장을 실행하지 않는다. S-DoT 최근 1만 행, 전국 주차 기능별 최대 5천 행, CCTV 회차당 1회 요청 및 재시도 없음 정책을 유지한다. 저장된 CCTV probe를 사용하는 경우에도 파일 경로는 호출자가 전달한다.

`official_files.py`는 공식 TOPIS 도로·ID 매핑 XLSX와 121장소 SHP를 읽는다. 공통 `read_xlsx_rows()`는 열 위치·행 순서를 보존하며 도시데이터 장소 목록과 S-DoT 설치 목록에서도 사용한다. SHP의 좌표계·121개 고유 장소·닫힌 링을 확인하고 모든 링을 전달한다. 도로 ID·정점 순서 등 값 검증은 `normalizers/official_files.py`에서 수행한다.
