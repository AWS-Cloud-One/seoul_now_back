# AWS 첫 설정: 네트워크와 저장소 만들기

이 단계는 EKS와 RDS를 배치할 **네트워크와 빈 저장소를 먼저 만드는 작업**이다. IAM 사용자로 로그인한 뒤 CloudFormation에 파일 하나를 올려 적용할 수 있다. 로컬 AWS CLI나 액세스 키가 없어도 된다.

준비 파일: [foundation.yaml](../cloudformation/foundation.yaml)

## 1. 이번에 만들어지는 것

| 구성 | 개수 | 이후 사용할 곳 |
| --- | --- | --- |
| VPC | 1 | 프로젝트 전용 네트워크 |
| 퍼블릭 서브넷 | 2 AZ에 각 1개 | ALB·NAT 배치 |
| 프라이빗 앱 서브넷 | 2 AZ에 각 1개 | EKS 노드·API·수집 배치 |
| 프라이빗 DB 서브넷 | 2 AZ에 각 1개 | RDS 배치 |
| Internet Gateway·라우트 테이블 | 1개·4개 | 서브넷별 통신 경로 |
| S3 Gateway endpoint | 1 | 앱 서브넷에서 S3로 접근하는 경로 |
| 비공개 ECR 저장소 | 1 | 백엔드 Docker 이미지 보관 |
| 비공개 S3 버킷 | 2 | 웹 빌드 파일, 허용된 자료 보관 |

EKS·EC2 인스턴스·RDS·ALB·CloudFront·IAM 사용자는 이 템플릿에 없다. 기본값 `NatMode=none`에서는 NAT Gateway와 Elastic IP도 생성하지 않는다. 지금은 서버를 가동하는 단계가 아니라 서버가 들어갈 기반을 준비하는 단계다.

S3는 공개 차단·암호화·버전 관리를 설정하고 HTTPS 요청만 허용한다. 웹 버킷도 아직 사이트 주소로 사용할 수 없다. 프론트 배포 때 CloudFront OAC와 읽기 정책을 추가한다.

ECR은 같은 이미지 태그를 덮어쓰지 못하도록 설정했다. 이후 `latest` 대신 커밋별 태그와 digest를 사용한다. 이미지 취약점 검사 설정은 계정의 ECR registry 설정을 확인한 뒤 따로 연결한다.

## 2. 콘솔에서 적용하기 — 처음에는 이 방법 사용

### 적용 전

1. 생성한 IAM 사용자로 AWS 콘솔에 로그인한다.
2. 리전을 **아시아 태평양(서울), `ap-northeast-2`**로 선택한다. 할당 계정이 다른 리전만 허용하면 그 리전과 AZ로 바꾼다.
3. VPC 콘솔에서 `10.42.0.0/16`이 연결 예정인 기존 네트워크와 겹치지 않는지 확인한다. 필요하면 다른 `10.x.0.0/16`을 선택한다.
4. 사용하는 IAM 사용자/역할에 CloudFormation 스택 관리 및 이번 EC2 네트워크·ECR·S3 자원을 생성·조회·변경·정리할 권한이 있는지 확인한다. IAM 사용자 생성만으로 배포 권한이 생기지는 않는다. 권한 부족 시 실패한 API 작업을 확인해 담당자에게 필요한 범위를 요청한다.

이 템플릿은 IAM 역할·정책을 생성하지 않아 `CAPABILITY_IAM` 동의가 필요하지 않다. 조직의 SCP나 교육 계정 제한은 사용자 정책과 별도로 적용될 수 있다.

### CloudFormation 화면

1. **CloudFormation → 스택 생성 → 새 리소스 사용**으로 들어간다.
2. 템플릿 파일 업로드를 선택하고 `infra/cloudformation/foundation.yaml`을 올린다.
3. 스택 이름과 파라미터를 아래처럼 입력한다.

| 입력 | 처음 사용할 값 |
| --- | --- |
| 스택 이름 | `seoul-now-lab-foundation` |
| ProjectName | `seoul-now` |
| Environment | `lab` |
| VpcCidr | `10.42.0.0/16` — 중복 확인 후 사용 |
| AvailabilityZoneA | `ap-northeast-2a` |
| AvailabilityZoneB | `ap-northeast-2c` |
| NatMode | **`none`** |

4. AZ는 해당 계정에서 사용 가능한 **서로 다른 표준 AZ**를 선택한다. 서울 외 리전이면 위 AZ 이름을 그대로 사용하지 않는다.
5. 스택 옵션은 기본값을 사용한다. 태그에는 `Project=seoul-now`, `Environment=lab`을 추가할 수 있다.
6. 검토 화면에서 템플릿·리전·파라미터를 확인하고 스택을 생성한다.
7. 이벤트 탭에서 진행 상황을 확인하고 **`CREATE_COMPLETE`**가 될 때까지 기다린다.

콘솔 절차의 기준은 [AWS CloudFormation 스택 생성 안내](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/cfn-console-create-stack.html)다.

같은 이름의 스택이나 ECR 저장소·S3 버킷이 이미 있으면 기존 자원을 지우지 않는다. 기존 프로젝트 자원인지 확인하고 해당 스택을 업데이트하거나 별도 프로젝트/환경 이름을 사용한다. 웹·자료 버킷명에는 계정 ID와 리전이 자동 포함된다.

## 3. 만들어진 결과 확인

CloudFormation 스택의 **출력(Outputs)** 탭에 다음 값이 나온다. 이후 EKS·RDS 설정에서 그대로 사용한다.

| 출력 | 다음에 연결할 곳 |
| --- | --- |
| `VpcId` | EKS·RDS·ALB가 사용할 VPC |
| `PublicSubnetIds` | ALB용 서브넷 |
| `AppSubnetIds` | EKS 클러스터·관리형 노드 그룹용 서브넷 |
| `DbSubnetIds` | RDS DB subnet group용 서브넷 |
| `BackendRepositoryUri` | Docker 이미지 push 주소 |
| `WebBucketName` | React `dist/` 업로드 위치 |
| `DataBucketName` | 허용된 자료 보관 위치 |
| `NatMode` | 현재 프라이빗 앱 서브넷의 외부 송신 구성 |

기본 CIDR의 서브넷 배치는 다음과 같다. VPC CIDR을 바꾸면 같은 순서로 자동 계산된다.

| 용도 | AZ A | AZ B |
| --- | --- | --- |
| 퍼블릭 | `10.42.0.0/20` | `10.42.16.0/20` |
| 앱 | `10.42.32.0/20` | `10.42.48.0/20` |
| DB | `10.42.64.0/20` | `10.42.80.0/20` |

라우팅과 저장소도 확인한다.

- 퍼블릭 서브넷만 Internet Gateway를 향하는 기본 경로를 갖는다. EC2의 public IP 자동 할당은 모든 서브넷에서 꺼져 있다.
- `NatMode=none`에서는 앱 서브넷에 인터넷 기본 경로가 없고, S3 endpoint 경로만 추가된다.
- DB 서브넷에는 인터넷 기본 경로와 로드밸런서 검색용 태그가 없다.
- ECR 저장소가 비공개이고 이미지 태그가 immutable인지 확인한다.
- S3 두 버킷의 공개 접근 차단·암호화·버전 관리가 활성화됐는지 확인한다.

## 4. EKS를 만들기 직전에 할 일

**지금 설정은 `NatMode=none`이므로, 곧바로 프라이빗 EC2 노드를 추가하면 필요한 외부 서비스에 접근할 수 없다.** S3 endpoint 하나가 ECR API·외부 공공 API·Secrets Manager 등의 경로까지 제공하지는 않는다.

EKS·노드를 생성하는 단계에서 같은 foundation 스택의 `NatMode`를 변경한다.

| 값 | 동작 | 사용할 단계 |
| --- | --- | --- |
| `none` | NAT·EIP 없음, 앱의 일반 인터넷 송신 없음 | 지금의 기반 준비 |
| `single` | 앱 A/B가 AZ A의 NAT 하나 사용 | 최초 연결 실습. AZ A의 송신 장애가 양쪽에 영향 |
| `per-az` | 앱 A/B가 각자 AZ의 NAT 사용 | AZ 분산·복구 실습 |

NAT를 켜면 사용 시간·처리량·public IPv4에 따른 비용이 발생한다. `single`은 다른 AZ에서 접근할 때 전송 비용도 고려한다. EKS 배포 후 NAT를 다시 끄면 노드의 이미지 pull과 앱의 외부 호출 등에 영향을 줄 수 있으므로 단순한 비용 스위치로 다루지 않는다. [NAT Gateway 설명](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-nat-gateway.html), [VPC 요금](https://aws.amazon.com/vpc/pricing/)

이후 순서는 **EKS와 노드/IAM → RDS/PostGIS → API·수집 배포 → ALB·CloudFront 연결**이다. 앱·DB 보안 그룹과 실제 접근 권한은 해당 실행 자원을 만들 때 연결한다.

## 5. CloudShell로 적용하는 대안

콘솔 업로드가 편하면 이 절은 건너뛰어도 된다. CloudShell을 사용하면 AWS CLI가 있는 브라우저 터미널에서 명령을 실행할 수 있다. 키를 채팅이나 프로젝트 파일에 붙여넣을 필요가 없다. [AWS CloudShell 설명](https://docs.aws.amazon.com/cloudshell/latest/userguide/welcome.html)

CloudShell에 다음 두 파일을 같은 디렉터리로 업로드한다.

- `foundation.yaml`
- [seoul-lab.foundation.parameters.json](../environments/seoul-lab.foundation.parameters.json)

아래는 **새 스택을 처음 만드는 경우**의 명령이다. 리전·AZ·CIDR을 바꾸었다면 JSON도 함께 수정한다.

```bash
aws sts get-caller-identity

aws cloudformation validate-template \
  --region ap-northeast-2 \
  --template-body file://foundation.yaml

aws cloudformation create-change-set \
  --region ap-northeast-2 \
  --stack-name seoul-now-lab-foundation \
  --change-set-name foundation-initial \
  --change-set-type CREATE \
  --template-body file://foundation.yaml \
  --parameters file://seoul-lab.foundation.parameters.json \
  --tags Key=Project,Value=seoul-now Key=Environment,Value=lab

aws cloudformation wait change-set-create-complete \
  --region ap-northeast-2 \
  --stack-name seoul-now-lab-foundation \
  --change-set-name foundation-initial

aws cloudformation describe-change-set \
  --region ap-northeast-2 \
  --stack-name seoul-now-lab-foundation \
  --change-set-name foundation-initial \
  --query 'Changes[].ResourceChange.{Action:Action,Id:LogicalResourceId,Type:ResourceType,Replacement:Replacement}' \
  --output table
```

여기까지는 실제 프로젝트 자원을 생성하기 전의 변경 내역 확인이다. 계정·리전·생성 목록이 맞으면 다음 명령으로 적용한다.

```bash
aws cloudformation execute-change-set \
  --region ap-northeast-2 \
  --stack-name seoul-now-lab-foundation \
  --change-set-name foundation-initial

aws cloudformation wait stack-create-complete \
  --region ap-northeast-2 \
  --stack-name seoul-now-lab-foundation

aws cloudformation describe-stacks \
  --region ap-northeast-2 \
  --stack-name seoul-now-lab-foundation \
  --query 'Stacks[0].Outputs' \
  --output table
```

이미 스택이 존재한다면 새 `CREATE`를 반복하지 않고 기존 스택에 대한 `UPDATE` 변경 세트를 만든다. 변경 세트 작성 자체가 `FAILED`이면 `StatusReason`을 확인한다. 실제 자원 생성이 실패했다면 CloudFormation 이벤트에서 첫 실패 자원의 원인을 확인한다.

## 6. 보존과 비용

S3 버킷·버킷 정책과 ECR은 스택을 삭제해도 보존하도록 정의했다. 스택 삭제만으로 저장 자료가 지워지지 않으며, 보존된 저장소의 데이터 비용도 자동 종료되지 않는다. 보존 자원 이름으로 스택을 다시 만들면 이름 충돌이 날 수 있으므로 기존 자원의 재사용/가져오기는 별도로 처리한다.

기본 구성은 EKS·EC2·RDS·ALB·NAT를 실행하지 않는다. S3/ECR 저장·요청·버전 보관 등의 비용은 실제 사용에 따라 발생할 수 있으므로 전체를 무료라고 간주하지 않는다. 배포 때 템플릿 업로드용으로 CloudFormation이 사용하는 S3 버킷은 이 문서의 웹/자료 버킷과 별개일 수 있다.

## 7. 검증 범위

템플릿 수정 후 프로젝트 루트에서 다음 검사를 실행할 수 있다. 로컬 Python은 Dockerfile과 같은 3.14 버전을 사용한다. `cfn-lint`는 루트 `requirements-dev.txt`의 개발 도구로 관리하며, 배포용 이미지가 설치하는 `requirements.txt`에는 포함하지 않는다.

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/cfn-lint \
  --template infra/cloudformation/foundation.yaml \
  --regions ap-northeast-2
```

`cfn-lint`는 템플릿의 스키마·참조·조건 등을 검사한다. 계정 권한·리소스 할당량·실제 생성 성공은 AWS에서 스택을 적용하고 확인해야 한다. [AWS cfn-lint 안내](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/cfn-lint.html)

2026-11-03 로컬 검증: `cfn-lint 1.59.0`의 서울 리전 검사 통과. 예제 파라미터·서브넷 CIDR·세 NAT 모드의 생성 조건/라우트 참조·DB 기본 경로 부재·S3 공개 차단/보존·문서 링크를 확인했다. AWS 계정의 스택 생성과 런타임 통신은 아직 검증하지 않았다.
