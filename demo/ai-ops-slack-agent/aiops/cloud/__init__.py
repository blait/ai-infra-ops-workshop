"""Deployed-mode adapters (AWS Lambda / Step Functions side). Nothing here imports claude_agent_sdk.

  stores.py     DynamoDB approval store (conditional writes, thread/target locks), Slack event dedupe,
                mock world state for AIOPS_MODE=mock
  slack.py      Slack request signature check, Web API client with DRY_RUN, secrets/approvers loading
  handlers.py   Lambda entry points: Slack ingress / worker / interactivity, Gateway tool targets,
                Step Functions approval-flow steps, executor, notifier
"""
