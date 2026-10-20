import json

import pytest

from aiops.approvals import Approval, ApprovalService, ApprovalStore
from aiops.memory import MemoryActionSink
from aiops.pipeline import OpsPipeline
from aiops.tools import RunContext, build_tools
from aiops.tools.core import Caller, ToolCore

from .conftest import I1, REQUESTER, FakeRunner, call_tool


class FakeRemote:
    """Stands in for GatewayToolClient: runs the real ToolCore 'behind the gateway'."""

    def __init__(self, core: ToolCore, deny: set[str] | None = None):
        self.core = core
        self.deny = deny or set()
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool, args):
        self.calls.append((tool, json.loads(json.dumps(args))))
        info = {"tool": tool, "gateway_tool": f"t___{tool}", "ms": 1}
        if tool in self.deny:
            return {"ok": False, "code": "policy_denied", "error": "[policy_denied] denied"}, {**info, "outcome": "denied"}
        args = dict(args)
        caller = Caller.from_json(args.pop("caller", None))
        env = await self.core.call(tool, args, caller)
        return env, {**info, "outcome": "ok" if env.get("ok") else "tool_error"}


@pytest.fixture
def remote_pipeline(settings, tmp_path):
    runner = FakeRunner()
    local = OpsPipeline(settings, runner=FakeRunner())  # just to borrow backend/runbooks
    svc = ApprovalService(store=ApprovalStore(tmp_path / "gw.sqlite3"), runbooks=local.runbooks, backend=local.backend,
                          executor=None, audit=local.audit, notes=MemoryActionSink(None), approvers=frozenset())
    remote = FakeRemote(ToolCore(local.backend, local.runbooks, svc))
    p = OpsPipeline(settings, runner=runner, remote=remote)
    runner.deps = p.deps
    p._svc = svc
    return p


def test_remote_pipeline_has_no_approvals_or_executor(remote_pipeline):
    assert remote_pipeline.approvals is None and remote_pipeline.executor is None
    assert remote_pipeline.deps.remote is remote_pipeline.remote


async def test_read_tool_via_remote_registers_local_evidence(remote_pipeline):
    ctx = RunContext(thread_id="t-r", user_id=REQUESTER, route="investigate")
    tools = build_tools(ctx, remote_pipeline.deps)
    res = await tools["logs_error_summary"].handler({"service": "checkout-api"})
    text = res["content"][0]["text"]
    body = json.loads(text)
    assert body["evidence_id"] == "EV-01" and len(text) <= remote_pipeline.settings.tool_output_max_chars
    assert remote_pipeline.notes.get_evidence("t-r", "EV-01")["payload"] == text
    assert remote_pipeline.remote.calls == [("logs_error_summary", {"service": "checkout-api"})]
    assert ctx.gateway_calls[0]["outcome"] == "ok"
    events = [r["event"] for r in remote_pipeline.audit.read(thread_id="t-r")]
    assert "gateway_call" in events and "tool_result" in events


async def test_propose_gets_trusted_caller_injected(remote_pipeline):
    ctx = RunContext(thread_id="t-p", user_id=REQUESTER, route="change_request", channel_id="C9")
    tools = build_tools(ctx, remote_pipeline.deps)
    ev = await call_tool(tools, "logs_error_summary", {"service": "checkout-api"})
    out = await call_tool(tools, "runbook_propose", {
        "runbook_id": "restart-ec2-instance", "params": {"service": "checkout-api", "InstanceId": I1},
        "reason": "i-...0001에만 커넥션 누수 에러가 몰림", "evidence_ids": [ev["evidence_id"]],
        "caller": {"thread_id": "forged", "user_id": "UFORGED", "known_evidence_ids": ["EV-99"]}})
    assert out["status"] == "PENDING_APPROVAL"
    sent = remote_pipeline.remote.calls[-1][1]
    assert sent["caller"] == {"thread_id": "t-p", "user_id": REQUESTER, "channel_id": "C9",
                              "known_evidence_ids": [ev["evidence_id"]]}
    assert ctx.proposals == [out["approval_id"]]
    rec = ctx.proposal_records[out["approval_id"]]
    assert rec["requested_by"] == REQUESTER and rec["thread_id"] == "t-p"
    approval = remote_pipeline.approval_record(out["approval_id"], ctx)
    assert isinstance(approval, Approval) and approval.status == "PENDING" and approval.params["InstanceId"] == I1
    assert remote_pipeline.approval_record(None, ctx) is None


async def test_read_tools_never_get_caller(remote_pipeline):
    ctx = RunContext(thread_id="t-c", user_id=REQUESTER, route="investigate")
    tools = build_tools(ctx, remote_pipeline.deps)
    await tools["cloudwatch_alarm_status"].handler({"caller": {"thread_id": "x"}})
    assert "caller" not in remote_pipeline.remote.calls[-1][1]


async def test_evidence_get_stays_local(remote_pipeline):
    ctx = RunContext(thread_id="t-e", user_id=REQUESTER, route="investigate")
    tools = build_tools(ctx, remote_pipeline.deps)
    await call_tool(tools, "cloudwatch_alarm_status", {})
    n = len(remote_pipeline.remote.calls)
    got = await tools["evidence_get"].handler({"evidence_id": "EV-01"})
    assert not got.get("is_error") and len(remote_pipeline.remote.calls) == n


async def test_remote_denial_becomes_mcp_error(remote_pipeline):
    remote_pipeline.remote.deny.add("cost_daily_delta")
    ctx = RunContext(thread_id="t-d", user_id=REQUESTER, route="investigate")
    tools = build_tools(ctx, remote_pipeline.deps)
    res = await tools["cost_daily_delta"].handler({})
    assert res["is_error"] and "policy_denied" in res["content"][0]["text"]
    assert ctx.gateway_calls[-1]["outcome"] == "denied"
    assert ctx.evidence_created == []


async def test_cost_workflow_goes_through_remote(remote_pipeline):
    reply = await remote_pipeline.handle(thread_id="t-w", user_id=REQUESTER, text="어제 대비 비용이 튄 서비스는?")
    assert reply.route == "cost_report" and "EC2 - Other" in reply.text
    assert [c[0] for c in remote_pipeline.remote.calls] == ["cost_daily_delta"]
    assert reply.meta["gateway_calls"][0]["tool"] == "cost_daily_delta"


async def test_alarm_workflow_goes_through_remote(remote_pipeline):
    reply = await remote_pipeline.handle(thread_id="t-a", user_id=REQUESTER, text="알람 현황")
    assert reply.route == "alarm_report" and "ALARM" in reply.text
    assert remote_pipeline.remote.calls[-1] == ("cloudwatch_alarm_status", {"state": "ANY"})
