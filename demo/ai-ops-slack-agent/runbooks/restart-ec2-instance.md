---
id: restart-ec2-instance
title: EC2 인스턴스 1대 재시작
status: approved
ssm_document: AWS-RestartEC2Instance
ssm_document_version: $DEFAULT
risk: medium
summary: 커넥션 누수, 스레드 고갈처럼 프로세스를 다시 띄우면 완화되는 증상에 인스턴스 1대씩 적용. 근본 원인 해결이 아님
param.service: ^[a-z0-9-]{2,40}$
param.InstanceId: ^i-[0-9a-f]{8,17}$
ssm_params: InstanceId
verify_metric: 5xx_rate
verify_max: 1.0
verify_window_minutes: 10
verify_alarm_suffixes: 5xx-rate-high
---

# EC2 인스턴스 1대 재시작 (AWS-RestartEC2Instance)

## 언제 쓰나
- 특정 인스턴스 1대에서만 에러가 몰리고(대조군 인스턴스는 정상), 프로세스 상태(커넥션 풀, 스레드, 메모리)가
  원인으로 보일 때의 **완화책**입니다.
- 배포·설정 결함이 근본 원인이면 재시작 후 다시 재발합니다. 이때는 롤백을 함께 요청합니다.

## 이 문서가 실제로 하는 일 (2026-10-15 `aws ssm get-document`로 확인)
- `AWS-RestartEC2Instance`는 OS 재부팅이 아니라 **stop → start** 두 단계(`aws:changeInstanceState`)입니다.
- 그래서 인스턴스 스토어 데이터가 사라지고, EIP가 없으면 퍼블릭 IP가 바뀌며, 몇 분이 걸립니다.
- **Auto Scaling 그룹 소속이면** 정지된 인스턴스가 상태 검사에 실패해 ASG가 교체할 수 있습니다.
  승인 전에 사람이 `EnterStandby` 또는 `HealthCheck` 프로세스 일시 중지 여부를 정합니다.

## 사전 점검 (제안 전에 근거로 확인)
1. 대상 인스턴스가 해당 서비스 소속인지 (서비스 인벤토리에 있는 ID만 허용됩니다).
2. 에러가 그 인스턴스에 집중되는지 (`logs_error_summary`의 by_instance).
3. 나머지 인스턴스가 정상이라 1대를 빼도 용량이 버티는지 (대상 그룹 정상 호스트 수).
4. 같은 스레드에 진행 중인 다른 변경이 없는지 (한 번에 변경 하나).

## 파라미터
- `service`: 서비스 이름 (예: `checkout-api`)
- `InstanceId`: 인스턴스 **1개**. 여러 대를 한 번에 재시작하지 않습니다. 필요하면 검증 후 다음 대상을 따로 제안합니다.

## 실행 (사람 승인 후 실행기가 수행)
- SSM Automation `AWS-RestartEC2Instance`를 `InstanceId=[<id>]`로 실행합니다.
- 실행 ID와 승인자는 실행 태그(`aiops:approval_id`, `aiops:approved_by`)와 CloudTrail에 남습니다.

## 완료 조건 (검증)
- 실행 상태 `Success`
- 실행 후 10분 동안 `5xx_rate`가 1.0% 이하
- `<service>-5xx-rate-high` 알람이 `OK`
- 위 조건을 못 맞추면 "검증 실패"로 보고하고 다음 조치(롤백 등)를 사람에게 넘깁니다.

## 되돌리기
- 재시작 자체는 되돌릴 것이 없습니다. 재시작 중 대상 그룹 용량이 1대 줄어든다는 점만 주의합니다.
