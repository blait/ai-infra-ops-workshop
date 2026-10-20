"""AWS Lambda entry points for the deployed architecture (see README §2 diagram).

  slack_ingress       API GW POST /slack/events: signature, url_verification, dedupe, "조사 중…", SQS FIFO
  slack_worker        SQS FIFO (group = thread) -> AgentCore Runtime (session = thread) -> Slack update
  slack_interactivity API GW POST /slack/interactivity: approval buttons -> DynamoDB CAS -> SendTaskSuccess
  gateway_read        AgentCore Gateway Lambda target: read-only tools + runbook_list/get
  gateway_propose     AgentCore Gateway Lambda target: runbook_propose -> PENDING record + start approval flow
  gateway_decoy       AgentCore Gateway Lambda target that Cedar forbids (proves the policy; never reached)
  flow_step           Step Functions steps: register task token / expire / execution status / verify
  executor            Step Functions step: start the allowlisted SSM Automation (its own IAM role)
  notifier            Step Functions step: post the decision / result to the Slack thread

Every handler takes an optional `deps` so tests can inject fakes; in Lambda the deps are built
from environment variables once per container.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..approvals import ApprovalService
from ..audit import AuditLog
from ..errors import ApprovalError
from ..executor import SsmExecutor
from ..formatting import APPROVE_ACTION, REJECT_ACTION, decided_card_blocks, verify_text
from ..formatting import context as ctx_block
from ..router import classify
from ..runbooks import RunbookRegistry
from ..tools.core import Caller, ToolCore
from .slack import SlackClient, load_approvers, load_slack_secret, verify_signature

READ_TARGET_TOOLS = frozenset({"cloudwatch_alarm_status", "cloudwatch_metric_summary", "logs_error_summary",
                               "cloudtrail_recent_changes", "cost_daily_delta", "runbook_list", "runbook_get"})
PROPOSE_TARGET_TOOLS = frozenset({"runbook_propose"})
_MENTION = re.compile(r"<@[A-Z0-9]+(\|[^>]+)?>")
_ROUTE_ACK = {
    "investigate": "조사 중… 지표·로그·변경 이력을 모으고 있습니다",
    "change_request": "변경 요청 확인 중… 허용 목록 runbook과 근거를 대조합니다",
    "cost_report": "비용 리포트 생성 중…",
    "alarm_report": "알람 현황 조회 중…",
}


def session_id_for(channel: str, thread_ts: str) -> str:
    """Runtime session id (>= 33 chars required) = thread. Same string is the notes thread id."""
    return f"slack-thread-{channel}-{thread_ts}"


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# ============================================================================== dependencies
@dataclass
class Deps:
    """Lazily-built AWS clients and services. Tests replace attributes with fakes."""

    component: str = "lambda"
    audit: AuditLog | None = None
    slack: SlackClient | None = None
    store: Any = None
    sqs: Any = None
    sfn: Any = None
    agentcore: Any = None
    ec2: Any = None
    approvers: frozenset[str] | None = None
    signing_secret: str | None = None
    _settings: Any = None
    _extra: dict = field(default_factory=dict)

    @property
    def settings(self):
        if self._settings is None:
            from ..config import load_settings

            os.environ.setdefault("AIOPS_DATA_DIR", "/tmp/aiops")
            self._settings = load_settings()
        return self._settings

    def log(self, event: str, **fields) -> None:
        if self.audit is None:
            self.audit = AuditLog(None, stdout=True, component=self.component)
        self.audit.write(event, **fields)

    def get_slack(self) -> SlackClient:
        if self.slack is None:
            arn = _env("AIOPS_SLACK_SECRET_ARN")
            secret = load_slack_secret(arn) if arn else {}
            dry = (_env("AIOPS_SLACK_DRY_RUN", "1") or "1").lower() in {"1", "true", "yes", "on"}
            self.slack = SlackClient(secret.get("bot_token"), dry,
                                     log=lambda rec: self.log("slack_dry_run" if "slack_dry_run" in rec else "slack",
                                                              **rec))
        return self.slack

    def get_signing_secret(self) -> str:
        if self.signing_secret is None:
            arn = _env("AIOPS_SLACK_SECRET_ARN")
            return (load_slack_secret(arn) if arn else {}).get("signing_secret", "")
        return self.signing_secret

    def get_store(self):
        if self.store is None:
            from .stores import DynamoApprovalStore

            self.store = DynamoApprovalStore(_env("AIOPS_TABLE"))
        return self.store

    def client(self, name: str, attr: str, **kw):
        if getattr(self, attr, None) is None:
            import boto3
            from botocore.config import Config

            setattr(self, attr, boto3.client(name, config=Config(retries={"mode": "standard", "max_attempts": 3}, **kw)))
        return getattr(self, attr)

    def get_approvers(self) -> frozenset[str]:
        if self.approvers is not None:
            return self.approvers
        return load_approvers(_env("AIOPS_APPROVERS_PARAM", "/aiops-agent/approvers"))

    def backend(self):
        if "backend" not in self._extra:
            from ..tools.backends import build_backend

            state = None
            if self.settings.mode == "mock" and _env("AIOPS_TABLE"):
                from .stores import DynamoMockState

                state = DynamoMockState(_env("AIOPS_TABLE"))
            self._extra["backend"] = build_backend(self.settings, state=state)
        return self._extra["backend"]

    def runbooks(self) -> RunbookRegistry:
        if "runbooks" not in self._extra:
            self._extra["runbooks"] = RunbookRegistry(self.settings.runbooks_dir)
        return self._extra["runbooks"]

    def memory_sink(self):
        if "sink" not in self._extra:
            from ..memory import AgentCoreMemory, MemoryActionSink

            mid = _env("AIOPS_MEMORY_ID")
            self._extra["sink"] = MemoryActionSink(AgentCoreMemory(mid, os.environ.get("AWS_REGION", "us-east-1"))
                                                   if mid else None)
        return self._extra["sink"]

    def approval_service(self, *, executor=None, target_check=None, memory: bool = True,
                         approvers: bool = False) -> ApprovalService:
        s = self.settings
        from ..memory import MemoryActionSink

        return ApprovalService(
            store=self.get_store(), runbooks=self.runbooks(), backend=self.backend(), executor=executor,
            audit=self._audit(), notes=self.memory_sink() if memory else MemoryActionSink(None),
            approvers=self.get_approvers() if approvers else frozenset(),
            ttl_minutes=s.approval_ttl_minutes, require_distinct_approver=s.require_distinct_approver,
            automation_assume_role_arn=s.automation_assume_role_arn, verify_wait_seconds=0,
            verify_poll_seconds=max(s.verify_poll_seconds, 1.0) if s.mode == "aws" else 0.0,
            verify_timeout_seconds=min(s.verify_timeout_seconds, 600), display_tz=s.display_tz,
            target_check=target_check)

    def _audit(self) -> AuditLog:
        if self.audit is None:
            self.audit = AuditLog(None, stdout=True, component=self.component)
        return self.audit


_DEPS: dict[str, Deps] = {}


def _deps(component: str, deps: Deps | None) -> Deps:
    if deps is not None:
        return deps
    if component not in _DEPS:
        _DEPS[component] = Deps(component=component)
    return _DEPS[component]


# ============================================================================== HTTP helpers
def _http(status: int, body: Any = None, content_type: str = "application/json") -> dict:
    if body is None:
        return {"statusCode": status, "body": ""}
    return {"statusCode": status, "headers": {"Content-Type": content_type},
            "body": body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)}


def _raw_body(event: dict) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8")


def _headers(event: dict) -> dict:
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def _check_signature(event: dict, d: Deps) -> tuple[bool, bytes, dict, str]:
    raw, h = _raw_body(event), _headers(event)
    ok, why = verify_signature(d.get_signing_secret(), h.get("x-slack-request-timestamp"), raw,
                               h.get("x-slack-signature"))
    return ok, raw, h, why


# ============================================================================== Slack ingress (events)
def accept_event(ev: dict) -> bool:
    if ev.get("bot_id") or ev.get("subtype") not in (None, "thread_broadcast"):
        return False
    if ev.get("type") == "message" and ev.get("channel_type") != "im":
        return False  # channel messages arrive as app_mention
    return ev.get("type") in {"app_mention", "message"}


def slack_ingress(event: dict, context: Any = None, deps: Deps | None = None) -> dict:
    started = time.monotonic()
    marks: dict[str, int] = {}

    def mark(name: str) -> None:
        marks[name] = int((time.monotonic() - started) * 1000)

    d = _deps("slack-ingress", deps)
    ok, raw, h, why = _check_signature(event, d)
    mark("signature")
    if not ok:
        d.log("slack_signature_rejected", reason=why, path="events")
        return _http(401, {"error": "invalid signature"})
    body = json.loads(raw or b"{}")
    if body.get("type") == "url_verification":
        return _http(200, {"challenge": body.get("challenge", "")})
    if h.get("x-slack-retry-num"):
        d.log("slack_retry_ignored", retry=h.get("x-slack-retry-num"), reason=h.get("x-slack-retry-reason"))
        return _http(200, {"ok": True, "ignored": "retry"})
    if body.get("type") != "event_callback":
        return _http(200, {"ok": True, "ignored": body.get("type")})
    ev = body.get("event") or {}
    if not accept_event(ev):
        return _http(200, {"ok": True, "ignored": "event filtered"})
    event_id = body.get("event_id") or f"{ev.get('channel')}:{ev.get('ts')}"
    claimed = d.get_store().claim_event(event_id)
    mark("dedupe")
    if not claimed:
        d.log("slack_event_duplicate", event_id=event_id)
        return _http(200, {"ok": True, "ignored": "duplicate"})

    channel, user = ev["channel"], ev.get("user", "unknown")
    thread_ts = ev.get("thread_ts") or ev["ts"]
    text = _MENTION.sub("", ev.get("text", "")).strip()
    route = classify(text)
    ack_ts = d.get_slack().post(channel, thread_ts, _ROUTE_ACK.get(route.kind, "확인 중…"))
    mark("slack_ack")
    msg = {"channel": channel, "thread_ts": thread_ts, "user": user, "text": text, "ack_ts": ack_ts,
           "event_id": event_id, "route": route.kind, "received_at": time.time()}
    d.client("sqs", "sqs").send_message(QueueUrl=_env("AIOPS_QUEUE_URL"), MessageBody=json.dumps(msg, ensure_ascii=False),
                                        MessageGroupId=f"{channel}-{thread_ts}"[:128],
                                        MessageDeduplicationId=re.sub(r"[^A-Za-z0-9_-]", "_", event_id)[:128])
    mark("sqs")
    elapsed = int((time.monotonic() - started) * 1000)
    d.log("slack_event_enqueued", event_id=event_id, channel=channel, thread_ts=thread_ts, user=user,
          route=route.kind, ack_ts=ack_ts, elapsed_ms=elapsed, step_ms=marks)
    return _http(200, {"ok": True})


# ============================================================================== worker (SQS -> runtime)
def _invoke_runtime(d: Deps, session: str, payload: dict, on_progress: Callable[[str], None]) -> dict:
    client = d.client("bedrock-agentcore", "agentcore", read_timeout=900, connect_timeout=10)
    resp = client.invoke_agent_runtime(agentRuntimeArn=_env("AIOPS_RUNTIME_ARN"), runtimeSessionId=session,
                                       payload=json.dumps(payload, ensure_ascii=False).encode(),
                                       contentType="application/json", accept="text/event-stream")
    stream, result = resp["response"], None
    ctype = resp.get("contentType", "")
    if "text/event-stream" not in ctype:
        return json.loads(stream.read().decode())
    for line in stream.iter_lines():
        line = line.decode("utf-8") if isinstance(line, bytes) else line
        if not line.startswith("data:"):
            continue
        item = json.loads(line[5:].strip())
        if item.get("type") == "progress":
            on_progress(item.get("message", ""))
        elif item.get("type") == "result":
            result = item
    return result or {"status": "error", "error": "runtime stream ended without a result"}


def slack_worker(event: dict, context: Any = None, deps: Deps | None = None) -> dict:
    d = _deps("slack-worker", deps)
    for record in event.get("Records", []):
        msg = json.loads(record["body"])
        channel, thread_ts, ack_ts = msg["channel"], msg["thread_ts"], msg["ack_ts"]
        session = session_id_for(channel, thread_ts)
        slack = d.get_slack()
        head = "조사 중…"
        lines: list[str] = []
        last = [0.0]

        def progress(m: str) -> None:
            lines.append(m)
            if time.monotonic() - last[0] >= 3.0:
                last[0] = time.monotonic()
                slack.update(channel, ack_ts, head + "\n" + "\n".join(f"• {x}" for x in lines[-4:]))

        started = time.monotonic()
        d.log("runtime_invoke", session=session, user=msg.get("user"), route=msg.get("route"))
        try:
            out = _invoke_runtime(d, session, {"prompt": msg["text"], "user_id": msg.get("user"),
                                               "channel_id": channel, "stream": True}, progress)
        except Exception as exc:  # noqa: BLE001 - never leave the thread hanging
            d.log("runtime_error", session=session, error=repr(exc)[:500])
            slack.update(channel, ack_ts, f"처리 중 오류가 났습니다: {type(exc).__name__}")
            continue
        if out.get("status") != "success":
            d.log("runtime_error", session=session, error=out.get("error"))
            slack.update(channel, ack_ts, f"처리 중 오류가 났습니다: {out.get('error')}")
            continue
        approval_id = out.get("approval_id")
        if approval_id:
            store = d.get_store()
            rec = store.get(approval_id)
            if rec is None or rec.thread_id != session or rec.status != "PENDING":
                d.log("card_rejected", approval_id=approval_id, reason="record mismatch",
                      status=getattr(rec, "status", None))
                out["blocks"] = [b for b in (out.get("blocks") or []) if b.get("type") != "actions"]
                out["response"] += "\n(승인 카드 비활성: 기록 불일치)"
            else:
                # the approval card IS this message: bind the record to it (button replay protection)
                store.set_fields_if(approval_id, ("PENDING",), channel_id=channel, card_ts=ack_ts)
        slack.update(channel, ack_ts, out.get("response", ""), out.get("blocks"))
        meta = out.get("meta") or {}
        d.log("slack_reply_posted", session=session, route=out.get("route"), approval_id=approval_id,
              elapsed_s=round(time.monotonic() - started, 1), cost_usd=meta.get("cost_usd"),
              gateway_calls=len(meta.get("gateway_calls") or []),
              since_ingress_s=round(time.time() - float(msg.get("received_at", time.time())), 1))
    return {"batchItemFailures": []}


# ============================================================================== interactivity (approval buttons)
def slack_interactivity(event: dict, context: Any = None, deps: Deps | None = None) -> dict:
    started = time.monotonic()
    d = _deps("slack-interactivity", deps)
    ok, raw, _, why = _check_signature(event, d)
    if not ok:
        d.log("slack_signature_rejected", reason=why, path="interactivity")
        return _http(401, {"error": "invalid signature"})
    form = urllib.parse.parse_qs(raw.decode("utf-8"))
    payload = json.loads((form.get("payload") or ["{}"])[0])
    if payload.get("type") != "block_actions" or not payload.get("actions"):
        return _http(200)
    action = payload["actions"][0]
    if action.get("action_id") not in {APPROVE_ACTION, REJECT_ACTION}:
        return _http(200)
    decision = "approve" if action["action_id"] == APPROVE_ACTION else "reject"
    approval_id = action.get("value", "")
    user = (payload.get("user") or {}).get("id", "")
    channel = (payload.get("channel") or {}).get("id", "")
    message = payload.get("message") or {}
    card_ts = message.get("ts")
    thread_ts = message.get("thread_ts") or card_ts
    slack = d.get_slack()
    svc = d.approval_service(approvers=True)
    try:
        rec = svc.decide(approval_id, user, decision, channel_id=channel, card_ts=card_ts, source="slack_button")
    except ApprovalError as e:
        slack.ephemeral(channel, user, f"처리하지 않았습니다 [{e.code}]: {e.message}", thread_ts)
        d.log("approval_click_denied", approval_id=approval_id, user=user, code=e.code,
              elapsed_ms=int((time.monotonic() - started) * 1000))
        return _http(200)
    item = d.get_store().get_item(approval_id) or {}
    token = item.get("task_token")
    if token:
        _send_decision(d, token, rec.status, user)
    else:
        d.log("approval_token_pending", approval_id=approval_id)  # flow_step(register) will resume it
    rb = svc.runbooks.get(rec.runbook_id)
    from zoneinfo import ZoneInfo

    if card_ts:
        slack.update(channel, card_ts, f"{rec.status}: {approval_id}",
                     decided_card_blocks(rec, rb.title if rb else "-", rb.risk if rb else "-",
                                         ZoneInfo(d.settings.display_tz)))
    label = "승인" if rec.status == "APPROVED" else "거절"
    slack.post(channel, thread_ts, f"<@{user}>님이 `{approval_id}`를 {label}했습니다."
               + (" 실행기가 허용 목록 문서만 실행합니다(Step Functions)." if rec.status == "APPROVED" else " 실행하지 않습니다."))
    d.log("approval_click_accepted", approval_id=approval_id, user=user, status=rec.status, token_sent=bool(token),
          elapsed_ms=int((time.monotonic() - started) * 1000))
    return _http(200)


def _send_decision(d: Deps, token: str, status: str, user: str) -> None:
    sfn = d.client("stepfunctions", "sfn")
    try:
        sfn.send_task_success(taskToken=token, output=json.dumps(
            {"decision": "approve" if status == "APPROVED" else "reject", "decided_by": user}))
    except Exception as exc:  # noqa: BLE001 - token already used / timed out: the record is the source of truth
        d.log("send_task_success_failed", error=repr(exc)[:300])


# ============================================================================== Gateway Lambda targets
def _tool_name(context: Any) -> str:
    try:
        full = context.client_context.custom["bedrockAgentCoreToolName"]
    except (AttributeError, KeyError, TypeError):
        return ""
    return full.split("___", 1)[1] if "___" in full else full


def gateway_read(event: dict, context: Any = None, deps: Deps | None = None) -> dict:
    d = _deps("gateway-read", deps)
    name = _tool_name(context)
    if name not in READ_TARGET_TOOLS:
        d.log("gateway_tool_refused", tool=name)
        return {"ok": False, "code": "unknown_tool", "error": f"이 타깃에 없는 도구: {name!r}"}
    args = dict(event or {})
    args.pop("caller", None)
    core = ToolCore(d.backend(), d.runbooks(), None, d.settings.display_tz, d.settings.log_row_cap)
    env = asyncio.run(core.call(name, args, Caller(thread_id="gateway", user_id="gateway")))
    d.log("gateway_tool", tool=name, ok=env.get("ok"), kind=env.get("kind"), summary=env.get("summary"),
          error=env.get("error"), mode=d.settings.mode)
    return env


def gateway_propose(event: dict, context: Any = None, deps: Deps | None = None) -> dict:
    d = _deps("gateway-propose", deps)
    name = _tool_name(context)
    if name not in PROPOSE_TARGET_TOOLS:
        return {"ok": False, "code": "unknown_tool", "error": f"이 타깃에 없는 도구: {name!r}"}
    args = dict(event or {})
    caller = Caller.from_json(args.pop("caller", None))
    svc = d.approval_service()
    core = ToolCore(d.backend(), d.runbooks(), svc, d.settings.display_tz, d.settings.log_row_cap)
    env = asyncio.run(core.call("runbook_propose", args, caller))
    if env.get("ok"):
        approval_id = env["approval_id"]
        try:
            resp = d.client("stepfunctions", "sfn").start_execution(
                stateMachineArn=_env("AIOPS_STATE_MACHINE_ARN"), name=approval_id,
                input=json.dumps({"approval_id": approval_id, "thread_id": caller.thread_id}))
            d.get_store().set_fields(approval_id, sfn_execution_arn=resp["executionArn"])
        except Exception as exc:  # noqa: BLE001 - no flow = no way to execute: fail the proposal closed
            d.get_store().transition(approval_id, ("PENDING",), "FAILED",
                                     verify_json=json.dumps({"error": f"approval flow start failed: {exc}"}))
            d.log("approval_flow_start_failed", approval_id=approval_id, error=repr(exc)[:300])
            return {"ok": False, "code": "flow_start_failed", "error": "승인 흐름을 시작하지 못해 제안을 취소했습니다."}
    d.log("gateway_tool", tool=name, ok=env.get("ok"), approval_id=env.get("approval_id"), error=env.get("error"),
          thread_id=caller.thread_id, requested_by=caller.user_id)
    return env


def gateway_toolkit(event: dict, context: Any = None, deps: Deps | None = None) -> dict:
    """Read-only AWS MCP bridge. Its role, not a prompt, limits downstream AWS API permissions."""
    from ..aws_toolkit import ManagedAwsMcpClient

    if _tool_name(context) != "aws_mcp_read":
        return {"ok": False, "error": "unknown AWS Toolkit tool"}
    d = _deps("gateway-toolkit", deps)
    client = d._extra.get("toolkit") or ManagedAwsMcpClient(os.environ.get("AWS_REGION", "us-east-1"), timeout=100)
    result = client.read((event or {}).get("code"))
    d.log("aws_toolkit_call", tool="aws___run_script", ok=result.get("ok"))
    return result


def gateway_decoy(event: dict, context: Any = None, deps: Deps | None = None) -> dict:
    """Registered on the Gateway only so the Cedar `forbid` has something to forbid. If this log line
    ever appears, the policy engine is not enforcing. The role behind it denies every AWS action."""
    d = _deps("gateway-decoy", deps)
    d.log("DECOY_TOOL_INVOKED_POLICY_NOT_ENFORCED", tool=_tool_name(context), args=json.dumps(event)[:300])
    return {"ok": False, "code": "should_never_run", "error": "이 도구는 정책으로 금지돼 있어야 합니다."}


# ============================================================================== Step Functions steps
def _target_check_fn(d: Deps):
    def check(rec) -> dict:
        iid = rec.params.get("InstanceId")
        if not iid:
            return {"ok": True, "summary": "대상 인스턴스 없음"}
        try:
            res = d.client("ec2", "ec2").describe_instances(InstanceIds=[iid])
            inst = res["Reservations"][0]["Instances"][0]
            state = inst["State"]["Name"]
            return {"ok": state == "running", "instance": iid, "state": state,
                    "summary": f"{iid} 실제 상태 {state} (ec2:DescribeInstances)"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "instance": iid, "summary": f"{iid} 상태 조회 실패: {type(exc).__name__}"}

    return check


def flow_step(event: dict, context: Any = None, deps: Deps | None = None) -> dict:
    d = _deps("approval-flow", deps)
    step, approval_id = event.get("step"), event.get("approval_id", "")
    store = d.get_store()
    if step == "register":
        token = event["token"]
        if store.set_fields_if(approval_id, ("PENDING",), task_token=token):
            d.log("approval_waiting", approval_id=approval_id)
            return {"registered": True}
        rec = store.get(approval_id)
        d.log("approval_decided_before_token", approval_id=approval_id, status=getattr(rec, "status", None))
        if rec and rec.status in {"APPROVED", "REJECTED"}:
            _send_decision(d, token, rec.status, rec.decided_by or "?")
        else:
            d.client("stepfunctions", "sfn").send_task_failure(taskToken=token, error="NotPending",
                                                               cause=f"status={getattr(rec, 'status', None)}")
        return {"registered": False}
    if step == "expire":
        ok = store.transition(approval_id, ("PENDING", "APPROVED"), "EXPIRED")
        if ok:
            d.memory_sink().upsert_action(event.get("thread_id", ""), approval_id, status="EXPIRED")
        rec = store.get(approval_id)
        d.log("approval_expired", approval_id=approval_id, transitioned=ok, status=getattr(rec, "status", None))
        return {"status": getattr(rec, "status", None)}
    svc = d.approval_service(executor=_status_executor(d), target_check=_target_check_fn(d))
    if step == "status":
        st = svc.execution_status(approval_id)
        d.log("execution_poll", approval_id=approval_id, status=st.get("status"), terminal=st.get("terminal"))
        return st
    if step == "verify":
        started_at = datetime.fromisoformat(event["started_at"])
        final = asyncio.run(svc.complete_execution(approval_id, event["status"], started_at))
        v = final.verify or {}
        d.log("verify_done", approval_id=approval_id, status=final.status, result=v.get("result"),
              summary=v.get("summary"))
        return {"status": final.status, "result": v.get("result"), "summary": v.get("summary")}
    raise ValueError(f"unknown step {step!r}")


def _status_executor(d: Deps) -> SsmExecutor:
    """Read-only use of the SSM client (GetAutomationExecution); this role cannot start anything."""
    region = _env("AIOPS_TARGET_REGION") or os.environ.get("AWS_REGION", "us-east-1")
    return SsmExecutor(region=region, allowed_documents=frozenset(), principal="approval-flow (read only)",
                       client=d.client("ssm", "_ssm_ro"))


def executor(event: dict, context: Any = None, deps: Deps | None = None) -> dict:
    d = _deps("executor", deps)
    approval_id = event.get("approval_id", "")
    region = _env("AIOPS_TARGET_REGION") or os.environ.get("AWS_REGION", "us-east-1")
    allowed = frozenset(x for x in (_env("AIOPS_EXECUTOR_ALLOWED_INSTANCES", "") or "").split(",") if x)
    ex = SsmExecutor(region=region, allowed_documents=d.runbooks().allowed_documents,
                     allowed_targets=allowed, principal=_env("AIOPS_EXECUTOR_PRINCIPAL", "role/aiops-executor"),
                     client=d.client("ssm", "_ssm"))
    svc = d.approval_service(executor=ex, memory=False)
    try:
        rec, started_at, params = svc.start_execution(approval_id)
    except ApprovalError as e:
        d.log("executor_refused", approval_id=approval_id, code=e.code, error=e.message)
        return {"error": e.code, "message": e.message}
    d.log("executor_started", approval_id=approval_id, execution_id=rec.execution_id, parameters=params,
          principal=ex.principal)
    return {"execution_id": rec.execution_id, "started_at": started_at.isoformat(), "principal": ex.principal}


def notifier(event: dict, context: Any = None, deps: Deps | None = None) -> dict:
    d = _deps("notifier", deps)
    approval_id, kind = event.get("approval_id", ""), event.get("kind", "result")
    rec = d.get_store().get(approval_id)
    if rec is None or not rec.channel_id or rec.channel_id == "streamlit-console":
        d.log("notify_skipped", approval_id=approval_id, kind=kind, reason="no record or card")
        return {"posted": False}
    thread_ts = event.get("thread_ts") or _thread_ts_from(rec.thread_id) or rec.card_ts
    slack = d.get_slack()
    if kind == "expired":
        text = f"`{approval_id}` 승인 대기 시간(30분)이 지나 만료됐습니다. 실행하지 않았습니다."
    elif kind == "rejected":
        text = f"`{approval_id}` 거절됨. 실행하지 않았습니다."
    elif kind == "exec_error":
        text = f"`{approval_id}` 실행하지 않았습니다 [{event.get('error')}]: {event.get('message')}"
    else:
        text = verify_text(rec)
    slack.post(rec.channel_id, thread_ts, text)
    if rec.card_ts and kind == "result":
        slack.update(rec.channel_id, rec.card_ts, f"{rec.status}: {approval_id}", [ctx_block(
            f"최종 `{rec.status}` · 검증 `{(rec.verify or {}).get('result', '-')}` · 실행 `{rec.execution_id}`")])
    d.log("notify_posted", approval_id=approval_id, kind=kind, status=rec.status,
          verify=(rec.verify or {}).get("result"))
    return {"posted": True, "status": rec.status}


def _thread_ts_from(session: str) -> str | None:
    m = re.match(r"slack-thread-[A-Z0-9]+-(\d+\.\d+)$", session or "")
    return m.group(1) if m else None


# ============================================================================== init-phase warm-up
def _prewarm(component: str) -> None:
    """Slack gives 3 s for the HTTP response. On a cold container the first boto3 import, client
    creation and the Secrets Manager read cost ~2.5 s inside the handler (measured 2026-10-18), so the
    two Slack-facing functions do that work in the Lambda init phase instead (AIOPS_PREWARM)."""
    d = _deps(component, None)
    try:
        d.get_signing_secret()
        d.get_slack()
        store = d.get_store()
        # open the TLS connections now (first-request handshakes cost ~1 s otherwise)
        store.ddb.get_item(TableName=store.table, Key={"pk": {"S": "EVT#prewarm"}})
        if component == "slack-ingress":
            d.client("sqs", "sqs").get_queue_attributes(QueueUrl=_env("AIOPS_QUEUE_URL"),
                                                        AttributeNames=["QueueArn"])
        if component == "slack-interactivity":
            d.client("stepfunctions", "sfn")
            d.get_approvers()
            d.approval_service(approvers=True)
            d.memory_sink()
    except Exception as exc:  # noqa: BLE001 - warm-up must never break init
        print(json.dumps({"prewarm_failed": component, "error": repr(exc)[:300]}))


if os.environ.get("AIOPS_PREWARM"):
    _prewarm(os.environ["AIOPS_PREWARM"])
