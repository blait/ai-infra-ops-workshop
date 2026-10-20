# runbooks/ = 변경 허용 목록

- 이 폴더의 `status: approved` runbook에 적힌 `ssm_document`만 실행할 수 있습니다.
- 에이전트는 `runbook_list`로 한 줄 목록만 보고, 필요할 때 `runbook_get`으로 본문을 읽습니다(필요할 때 불러오기).
- 허용 목록 검사는 세 곳에서 따로 합니다: `runbook_propose` 도구, 승인 서비스(`aiops/approvals.py`), 실행기.
  IAM(`iam/approver-executor-policy.json`)도 같은 문서만 허용합니다.
- runbook 추가·승격은 이 폴더와 IAM 정책을 함께 바꾸는 PR로 리뷰합니다. 채팅이나 모델 출력으로는 바뀌지 않습니다.

front-matter 키

| 키 | 의미 |
|---|---|
| `id`, `title`, `summary` | 식별자와 설명 |
| `status` | `approved`(실행 가능) 또는 `draft`(목록에만 보임) |
| `ssm_document`, `ssm_document_version` | 실행할 SSM Automation 문서와 버전 |
| `param.<이름>` | 파라미터 정규식(전체 일치). 정의되지 않은 파라미터는 거부 |
| `ssm_params` | SSM에 넘길 파라미터 이름 |
| `verify_metric`, `verify_max`, `verify_window_minutes`, `verify_alarm_suffixes` | 실행 후 검증 기준 |
