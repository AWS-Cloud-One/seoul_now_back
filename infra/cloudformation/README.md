# CloudFormation 템플릿

| 템플릿 | 구성 | 상태 |
| --- | --- | --- |
| [foundation.yaml](foundation.yaml) | 2 AZ VPC·6개 서브넷·라우팅·S3 endpoint·ECR·비공개 S3 2개 | 코드 준비, 실제 계정 적용 전 |

처음에는 [AWS 첫 설정 안내](../runbooks/01-foundation.md)를 따른다. 예제 파라미터는 [서울 lab](../environments/seoul-lab.foundation.parameters.json)에 있다. 기본값 `NatMode=none`은 기반만 준비하며, EKS 노드를 배치하기 전에 NAT 또는 필요한 외부 접근 경로를 연결해야 한다.

EKS·관리형 노드 그룹·IAM·RDS·ALB/CloudFront 연결·관측 템플릿은 이후 추가한다. 다음 스택은 foundation의 `VpcId`, `AppSubnetIds`, `DbSubnetIds` 등의 출력을 입력받는다. 앱 코드와 Alembic 마이그레이션은 이곳에서 중복 관리하지 않는다.

NAT/리전/보존 정책을 포함한 자원 생성 조건은 템플릿과 runbook에서 함께 관리한다. 콘솔에서 개별 자원을 따로 수정하기보다 스택 변경 세트로 반영한다.
