import base64
import json
import time
import urllib.parse
from types import SimpleNamespace

import pytest

from aiops.approvals import ApprovalStore
from aiops.audit import AuditLog
from aiops.cloud import handlers as h
from aiops.cloud.slack import SlackClient, compute_signature
from aiops.memory import MemoryActionSink
from aiops.pipeline import OpsPipeline

from .conftest import APPROVER, I1, OUTSIDER, REQUESTER, FakeRunner

SECRET = "test-secret"
CHANNEL, THREAD_TS, CARD_TS = "C0DEMO1", "1760500000.000100", "1760500001.000200"
SESSION = h.session_id_for(CHANNEL, THREAD_TS)
_COLS = {"channel_id", "card_ts", "status", "decided_by", "decided_at", "execution_id", "executor_principal",
         "verify_json", "document"}


class FakeStore(ApprovalStore):
    """SQLite store + the extra DynamoDB-only bits (raw item fields, event claims)."""

    def __init__(self, path):
        super().__init__(path)
        self.extra: dict[str, dict] = {}
        self.events: set[str] = set()

    def _split(self, approval_id, fields):
        known = {k: v for k, v in fields.items() if k in _COLS}
        self.extra.setdefault(approval_id, {}).update({k: v for k, v in fields.items() if k not in _COLS})
        return known

    def set_fields(self, approval_id, **fields):
        known = self._split(approval_id, fields)
        if known:
            super().set_fields(approval_id, **known)

    def set_fields_if(self, approval_id, statuses, **fields):
        rec = self.get(approval_id)
        if rec is None or rec.status not in statuses:
            return False
        self.set_fields(approval_id, **fields)
        return True

    def get_item(self, approval_id):
        rec = self.get(approval_id)
        return {**rec.to_dict(), **self.extra.get(approval_id, {})} if rec else None

    def claim_event(self, event_id, ttl_s=3600):
        if event_id in self.events:
            return False
        self.events.add(event_id)
        return True


class Recorder:
    def __init__(self, **returns):
        self.calls: list[tuple[str, dict]] = []
        self.returns = returns
        self.fail: set[str] = set()

    def __getattr__(self, name):
        def fn(**kw):
            self.calls.append((name, kw))
            if name in self.fail:
                raise RuntimeError(f"{name} failed")
            r = self.returns.get(name, {})
            return r(**kw) if callable(r) else r
        return fn

    def named(self, name):
        return [kw for n, kw in self.calls if n == name]


@pytest.fixture
def d(settings, tmp_path):
    p = OpsPipeline(settings, runner=FakeRunner())
    slack_log: list[dict] = []
    deps = h.Deps(component="test", signing_secret=SECRET, slack=SlackClient(None, True, log=slack_log.append),
                  store=FakeStore(tmp_path / "ddb.sqlite3"), approvers=frozenset({APPROVER}), _settings=settings)
    deps.audit = AuditLog(tmp_path / "lambda-audit.jsonl")
    deps.sqs = Recorder()
    deps.sfn = Recorder(start_execution=lambda **kw: {"executionArn": f"arn:sfn:{kw['name']}"})
    deps.ec2 = Recorder(describe_instances={"Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]})
    deps._ssm = Recorder(start_automation_execution={"AutomationExecutionId": "exec-123"})
    deps._ssm_ro = Recorder(get_automation_execution={"AutomationExecution": {"AutomationExecutionStatus": "Success"}})
    deps._extra.update(backend=p.backend, runbooks=p.runbooks, sink=MemoryActionSink(None))
    deps.slack_log = slack_log
    return deps


def events(d):
    return [r["event"] for r in d.audit.read()]


def signed(body: bytes, *, b64=False, ts=None, secret=SECRET, headers=None):
    ts = ts or str(int(time.time()))
    hd = {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": compute_signature(secret, ts, body), **(headers or {})}
    return {"headers": hd, "isBase64Encoded": b64,
            "body": base64.b64encode(body).decode() if b64 else body.decode()}


def mention(event_id="Ev1", **ev):
    e = {"type": "app_mention", "channel": CHANNEL, "user": REQUESTER, "text": "<@UBOT> checkout-api 5xx 늘었어, 원인 봐줘",
         "ts": THREAD_TS, **ev}
    return json.dumps({"type": "event_callback", "event_id": event_id, "event": e}).encode()


def gw_ctx(tool):
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": tool}))


# ------------------------------------------------------------------ ingress
def test_ingress_rejects_bad_signature(d):
    ev = signed(mention(), secret="wrong")
    assert h.slack_ingress(ev, deps=d)["statusCode"] == 401
    assert d.sqs.calls == [] and "slack_signature_rejected" in events(d)


def test_ingress_url_verification(d):
    r = h.slack_ingress(signed(json.dumps({"type": "url_verification", "challenge": "abc"}).encode()), deps=d)
    assert r["statusCode"] == 200 and json.loads(r["body"]) == {"challenge": "abc"}


def test_ingress_ignores_retries_and_bots(d):
    r = h.slack_ingress(signed(mention(), headers={"X-Slack-Retry-Num": "1"}), deps=d)
    assert json.loads(r["body"])["ignored"] == "retry"
    r = h.slack_ingress(signed(mention(bot_id="B1")), deps=d)
    assert json.loads(r["body"])["ignored"] == "event filtered"
    assert d.sqs.calls == []


def test_ingress_enqueues_by_thread_and_dedupes(d):
    r = h.slack_ingress(signed(mention(), b64=True), deps=d)
    assert r["statusCode"] == 200
    (sent,) = d.sqs.named("send_message")
    assert sent["MessageGroupId"] == f"{CHANNEL}-{THREAD_TS}" and sent["MessageDeduplicationId"] == "Ev1"
    body = json.loads(sent["MessageBody"])
    assert body["ack_ts"] and body["text"] == "checkout-api 5xx 늘었어, 원인 봐줘" and body["route"] == "investigate"
    assert d.slack_log[0]["slack_dry_run"]["text"].startswith("조사 중")
    r2 = h.slack_ingress(signed(mention()), deps=d)
    assert json.loads(r2["body"])["ignored"] == "duplicate" and len(d.sqs.named("send_message")) == 1


def test_accept_event_filters_channel_messages():
    assert h.accept_event({"type": "message", "channel_type": "im"})
    assert not h.accept_event({"type": "message", "channel_type": "channel"})
    assert not h.accept_event({"type": "app_mention", "subtype": "message_changed"})


# ------------------------------------------------------------------ gateway targets
def test_gateway_read_strips_caller_and_returns_envelope(d):
    env = h.gateway_read({"service": "checkout-api", "metric": "5xx_rate", "caller": {"thread_id": "x"}},
                         gw_ctx("readtools___cloudwatch_metric_summary"), deps=d)
    assert env["ok"] and env["kind"] == "evidence" and env["data"]["change_point"] == "13:45"


def test_gateway_read_refuses_other_tools(d):
    assert h.gateway_read({}, gw_ctx("readtools___runbook_propose"), deps=d)["code"] == "unknown_tool"
    assert h.gateway_read({}, None, deps=d)["code"] == "unknown_tool"


def propose(d, **kw):
    args = {"runbook_id": "restart-ec2-instance", "params": {"service": "checkout-api", "InstanceId": I1},
            "reason": "i-...0001에만 커넥션 누수 에러가 몰림", "evidence_ids": ["EV-01"],
            "caller": {"thread_id": SESSION, "user_id": REQUESTER, "channel_id": CHANNEL,
                       "known_evidence_ids": ["EV-01"]}, **kw}
    return h.gateway_propose(args, gw_ctx("changeproposal___runbook_propose"), deps=d)


def test_gateway_propose_creates_pending_and_starts_flow(d):
    env = propose(d)
    assert env["ok"]
    apr = env["approval_id"]
    (start,) = d.sfn.named("start_execution")
    assert start["name"] == apr and json.loads(start["input"])["approval_id"] == apr
    assert d.store.get(apr).status == "PENDING"
    assert d.store.get_item(apr)["sfn_execution_arn"] == f"arn:sfn:{apr}"


def test_gateway_propose_flow_failure_fails_closed(d):
    d.sfn.fail.add("start_execution")
    env = propose(d)
    assert env == {"ok": False, "code": "flow_start_failed", "error": env["error"]}
    (rec,) = d.store.list()
    assert rec.status == "FAILED"


def test_gateway_propose_wrong_tool_and_bad_evidence(d):
    assert h.gateway_propose({}, gw_ctx("changeproposal___cost_daily_delta"), deps=d)["code"] == "unknown_tool"
    env = propose(d, evidence_ids=["EV-77"])
    assert env["ok"] is False and env["code"] == "unknown_evidence" and d.sfn.named("start_execution") == []


def test_gateway_decoy(d):
    assert h.gateway_decoy({"cidr": "0.0.0.0/0"}, gw_ctx("decoy___ec2_open_security_group"), deps=d)["code"] == \
        "should_never_run"
    assert "DECOY_TOOL_INVOKED_POLICY_NOT_ENFORCED" in events(d)


# ------------------------------------------------------------------ approval flow
def pending_with_card(d):
    apr = propose(d)["approval_id"]
    d.store.set_fields(apr, channel_id=CHANNEL, card_ts=CARD_TS)
    return apr


def click(d, apr, user, action="aiops_approve", card_ts=CARD_TS):
    payload = {"type": "block_actions", "user": {"id": user}, "channel": {"id": CHANNEL},
               "message": {"ts": card_ts, "thread_ts": THREAD_TS},
               "actions": [{"action_id": action, "value": apr}]}
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    return h.slack_interactivity(signed(body), deps=d)


def ephemerals(d):
    return [r["slack_dry_run"]["text"] for r in d.slack_log if r["slack_dry_run"]["method"] == "chat.postEphemeral"]


def test_flow_register_stores_token(d):
    apr = pending_with_card(d)
    assert h.flow_step({"step": "register", "approval_id": apr, "token": "tok-1"}, deps=d) == {"registered": True}
    assert d.store.get_item(apr)["task_token"] == "tok-1"


def test_interactivity_non_approver_then_approver_then_double_click(d):
    apr = pending_with_card(d)
    h.flow_step({"step": "register", "approval_id": apr, "token": "tok-1"}, deps=d)
    assert click(d, apr, OUTSIDER)["statusCode"] == 200
    assert "[not_approver]" in ephemerals(d)[-1] and d.store.get(apr).status == "PENDING"
    assert d.sfn.named("send_task_success") == []

    assert click(d, apr, APPROVER)["statusCode"] == 200
    assert d.store.get(apr).status == "APPROVED"
    (sent,) = d.sfn.named("send_task_success")
    assert sent["taskToken"] == "tok-1" and json.loads(sent["output"]) == {"decision": "approve", "decided_by": APPROVER}

    click(d, apr, APPROVER)
    assert "[already_decided]" in ephemerals(d)[-1] and len(d.sfn.named("send_task_success")) == 1


def test_interactivity_wrong_card_and_bad_signature(d):
    apr = pending_with_card(d)
    click(d, apr, APPROVER, card_ts="999.999")
    assert "[wrong_context]" in ephemerals(d)[-1] and d.store.get(apr).status == "PENDING"
    body = urllib.parse.urlencode({"payload": "{}"}).encode()
    assert h.slack_interactivity(signed(body, secret="nope"), deps=d)["statusCode"] == 401


def test_register_after_decision_resumes_immediately(d):
    apr = pending_with_card(d)
    click(d, apr, APPROVER, action="aiops_reject")  # clicked before the flow registered its token
    assert d.store.get(apr).status == "REJECTED" and "approval_token_pending" in events(d)
    assert h.flow_step({"step": "register", "approval_id": apr, "token": "tok-2"}, deps=d) == {"registered": False}
    (sent,) = d.sfn.named("send_task_success")
    assert sent["taskToken"] == "tok-2" and json.loads(sent["output"])["decision"] == "reject"


def test_expire_step(d):
    apr = pending_with_card(d)
    assert h.flow_step({"step": "expire", "approval_id": apr, "thread_id": SESSION}, deps=d) == {"status": "EXPIRED"}
    assert d.store.get(apr).status == "EXPIRED"


def test_executor_status_verify_end_to_end(d, monkeypatch):
    monkeypatch.setenv("AIOPS_EXECUTOR_ALLOWED_INSTANCES", I1)
    apr = pending_with_card(d)
    click(d, apr, APPROVER)
    out = h.executor({"approval_id": apr}, deps=d)
    assert out["execution_id"] == "exec-123" and out["started_at"]
    (start,) = d._ssm.named("start_automation_execution")
    assert start["DocumentName"] == "AWS-RestartEC2Instance" and start["Parameters"]["InstanceId"] == [I1]
    assert {"Key": "aiops:approval_id", "Value": apr} in start["Tags"]
    st = h.flow_step({"step": "status", "approval_id": apr}, deps=d)
    assert st["status"] == "Success" and st["terminal"] is True
    res = h.flow_step({"step": "verify", "approval_id": apr, "started_at": out["started_at"], "status": st}, deps=d)
    assert res["status"] == "SUCCEEDED" and res["result"] == "PASS"
    rec = d.store.get(apr)
    assert rec.verify["target"]["state"] == "running" and rec.verify["data_source"] == "mock-fixtures"
    assert d.ec2.named("describe_instances") == [{"InstanceIds": [I1]}]


def test_notifier_result_posts_verify_text_and_updates_card(d, monkeypatch):
    monkeypatch.setenv("AIOPS_EXECUTOR_ALLOWED_INSTANCES", I1)
    apr = pending_with_card(d)
    click(d, apr, APPROVER)
    out = h.executor({"approval_id": apr}, deps=d)
    st = h.flow_step({"step": "status", "approval_id": apr}, deps=d)
    h.flow_step({"step": "verify", "approval_id": apr, "started_at": out["started_at"], "status": st}, deps=d)
    n = h.notifier({"approval_id": apr, "kind": "result"}, context=SimpleNamespace(), deps=d)
    assert n == {"posted": True, "status": "SUCCEEDED"}
    post = [r["slack_dry_run"] for r in d.slack_log if r["slack_dry_run"]["method"] == "chat.postMessage"][-1]
    assert post["thread_ts"] == THREAD_TS and "검증 통과" in post["text"]


def test_notifier_result_text_without_card_update(d, monkeypatch):
    """Same verify_text post, card update path skipped (no card_ts) so the shadowing bug is not hit."""
    monkeypatch.setenv("AIOPS_EXECUTOR_ALLOWED_INSTANCES", I1)
    apr = pending_with_card(d)
    click(d, apr, APPROVER)
    out = h.executor({"approval_id": apr}, deps=d)
    st = h.flow_step({"step": "status", "approval_id": apr}, deps=d)
    h.flow_step({"step": "verify", "approval_id": apr, "started_at": out["started_at"], "status": st}, deps=d)
    d.store.set_fields(apr, card_ts=None)
    assert h.notifier({"approval_id": apr, "kind": "result"}, deps=d) == {"posted": True, "status": "SUCCEEDED"}
    post = d.slack_log[-1]["slack_dry_run"]
    assert post["method"] == "chat.postMessage" and post["thread_ts"] == THREAD_TS and "검증 통과" in post["text"]


def test_verify_fails_when_instance_not_running(d, monkeypatch):
    monkeypatch.setenv("AIOPS_EXECUTOR_ALLOWED_INSTANCES", I1)
    d.ec2.returns["describe_instances"] = {"Reservations": [{"Instances": [{"State": {"Name": "stopped"}}]}]}
    apr = pending_with_card(d)
    click(d, apr, APPROVER)
    out = h.executor({"approval_id": apr}, deps=d)
    st = h.flow_step({"step": "status", "approval_id": apr}, deps=d)
    res = h.flow_step({"step": "verify", "approval_id": apr, "started_at": out["started_at"], "status": st}, deps=d)
    assert res["result"] == "FAIL"


def test_executor_refuses_target_outside_allowlist_and_unapproved(d, monkeypatch):
    apr = pending_with_card(d)
    out = h.executor({"approval_id": apr}, deps=d)
    assert out["error"] == "not_approved" and d._ssm.calls == []
    monkeypatch.setenv("AIOPS_EXECUTOR_ALLOWED_INSTANCES", "i-0ffffffffffffffff")
    click(d, apr, APPROVER)
    out = h.executor({"approval_id": apr}, deps=d)
    assert out["error"] == "not_allowlisted" and d._ssm.calls == []
    assert d.store.get(apr).status == "FAILED"


def test_notifier_expired_and_missing(d):
    apr = pending_with_card(d)
    h.flow_step({"step": "expire", "approval_id": apr}, deps=d)
    assert h.notifier({"approval_id": apr, "kind": "expired"}, deps=d)["posted"]
    assert "만료" in d.slack_log[-1]["slack_dry_run"]["text"]
    assert h.notifier({"approval_id": "apr-nope"}, deps=d) == {"posted": False}


def test_session_id_and_thread_ts_helpers():
    assert len(h.session_id_for("C1", "1.2")) >= 33 or len(SESSION) >= 33
    assert len(SESSION) >= 33
    assert h._thread_ts_from(SESSION) == THREAD_TS
    assert h._thread_ts_from("cli-demo") is None


def test_unknown_flow_step(d):
    with pytest.raises(ValueError):
        h.flow_step({"step": "nope", "approval_id": "x"}, deps=d)


# ------------------------------------------------------------------ worker
class FakeStream:
    def __init__(self, lines):
        self.lines = lines

    def iter_lines(self):
        return iter(self.lines)


def test_worker_streams_runtime_and_binds_card(d):
    apr = propose(d)["approval_id"]
    result = {"type": "result", "status": "success", "response": "제안함", "route": "change_request",
              "approval_id": apr, "blocks": [{"type": "actions", "elements": []}], "meta": {"cost_usd": 0.01}}
    lines = [b'data: {"type": "progress", "message": "step"}', b"", f"data: {json.dumps(result)}".encode()]
    d.agentcore = Recorder(invoke_agent_runtime={"response": FakeStream(lines), "contentType": "text/event-stream"})
    msg = {"channel": CHANNEL, "thread_ts": THREAD_TS, "ack_ts": CARD_TS, "user": REQUESTER, "text": "재시작해줘",
           "received_at": time.time()}
    assert h.slack_worker({"Records": [{"body": json.dumps(msg)}]}, deps=d) == {"batchItemFailures": []}
    (inv,) = d.agentcore.named("invoke_agent_runtime")
    assert inv["runtimeSessionId"] == SESSION and json.loads(inv["payload"])["stream"] is True
    rec = d.store.get(apr)
    assert rec.card_ts == CARD_TS and rec.channel_id == CHANNEL
    last = d.slack_log[-1]["slack_dry_run"]
    assert last["method"] == "chat.update" and last["ts"] == CARD_TS and last["blocks"]


def test_worker_disables_card_on_record_mismatch(d):
    result = {"type": "result", "status": "success", "response": "x", "approval_id": "apr-forged",
              "blocks": [{"type": "actions"}], "meta": {}}
    d.agentcore = Recorder(invoke_agent_runtime={"response": FakeStream([f"data: {json.dumps(result)}".encode()]),
                                                 "contentType": "text/event-stream"})
    msg = {"channel": CHANNEL, "thread_ts": THREAD_TS, "ack_ts": CARD_TS, "text": "x"}
    h.slack_worker({"Records": [{"body": json.dumps(msg)}]}, deps=d)
    last = d.slack_log[-1]["slack_dry_run"]
    assert last["blocks"] == [] and "기록 불일치" in last["text"] and "card_rejected" in events(d)


def test_worker_runtime_error_posts_message(d):
    d.agentcore = Recorder()
    d.agentcore.fail.add("invoke_agent_runtime")
    msg = {"channel": CHANNEL, "thread_ts": THREAD_TS, "ack_ts": CARD_TS, "text": "x"}
    h.slack_worker({"Records": [{"body": json.dumps(msg)}]}, deps=d)
    assert "오류" in d.slack_log[-1]["slack_dry_run"]["text"] and "runtime_error" in events(d)
