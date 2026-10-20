---
id: rollback-codedeploy-deployment
title: CodeDeploy 배포 중지 및 이전 버전으로 롤백 (초안, 실행 불가)
status: draft
ssm_document: Ops-RollbackCodeDeployDeployment
ssm_document_version: 1
risk: high
summary: 배포 직후 장애에서 직전 리비전으로 되돌림. 사내 SSM 문서 검토가 끝나지 않아 허용 목록에 없음
param.service: ^[a-z0-9-]{2,40}$
param.DeploymentId: ^d-[A-Z0-9]{9}$
ssm_params: DeploymentId
---

# CodeDeploy 롤백 (초안)

이 runbook은 **검토 중**이라 허용 목록에 없습니다. 에이전트는 이 runbook을 제안할 수 없고,
제안하더라도 승인 서비스와 실행기가 모두 거부합니다.

배포 결함이 근본 원인으로 보이면 다음처럼 사람에게 넘깁니다.
- 배포 파이프라인 담당 팀에 롤백 요청 (CodeDeploy 콘솔 또는 파이프라인의 롤백 작업)
- 조사 노트의 근거 ID(EV-..)를 함께 전달

허용 목록에 올리려면: 사내 SSM 문서 `Ops-RollbackCodeDeployDeployment` 작성 → 보안 검토 →
이 파일의 `status: approved` 변경과 실행 역할 IAM 정책 갱신을 같은 PR로 리뷰합니다.
