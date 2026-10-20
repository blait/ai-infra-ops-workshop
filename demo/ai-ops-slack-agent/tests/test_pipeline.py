import asyncio

from aiops import prompts
from aiops.agent import RunResult, gate_rca
from aiops.formatting import APPROVE_ACTION, REJECT_ACTION
from aiops.tools import READ_TOOLS

from .conftest import I1, REQUESTER, call_tool

RCA = {
    "service": "checkout-api", "summary": "v2.14.0 배포 후 i-...0001 커넥션 누수",
    "hypothesis": "v2.14.0의 커넥션 누수로 풀 고갈", "confidence": "high",
    "evidence": [{"id": "EV-01", "fact": "0001에 에러 97%"}, {"id": "EV-99", "fact": "지어낸 근거"}],
    "timeline": [{"at": "13:42", "event": "배포", "evidence_id": "EV-03"}],
    "ruled_out": [{"item": "보안그룹 변경", "reason": "증상 이후", "evidence_ids": ["EV-03", "EV-42"]}],
    "next_checks": ["롤백 검토"], "missing_data": [],
}


def investigator_fake(rca=RCA):
    async def fn(spec, ctx, tools):
        await call_tool(tools, "logs_error_summary", {"service": "checkout-api"})
        await call_tool(tools, "cloudwatch_metric_summary", {"service": "checkout-api", "metric": "5xx_rate"})
        await call_tool(tools, "cloudtrail_recent_changes", {"service": "checkout-api"})
        return RunResult(name=spec.name, structured=rca, cost_usd=0.1)
    return fn


async def evaluator_fake(spec, ctx, tools):
    assert spec.tools == [] and spec.agents is None  # evaluator has no tools
    assert "HikariPool" in spec.prompt  # it sees the raw evidence payloads, not just the draft
    return RunResult(name=spec.name, cost_usd=0.02, structured={
        "verdict": "revise", "max_supported_confidence": "medium", "reviewer_note": "인스턴스별 5xx 미확인",
        "issues": [{"type": "overconfidence", "problem": "대조군만으로 high는 과함", "fix": "medium"}]})


async def reviser_fake(spec, ctx, tools):
    return RunResult(name=spec.name, structured=dict(RCA, confidence="high", evidence=[{"id": "EV-01", "fact": "f"}]),
                     cost_usd=0.01)


async def test_investigate_runs_generator_then_independent_evaluator(pipeline, runner):
    runner.on("investigator", investigator_fake())
    runner.on("evaluator", evaluator_fake)
    runner.on("reviser", reviser_fake)
    reply = await pipeline.handle(thread_id="p1", user_id=REQUESTER, text="checkout-api 5xx 늘었어, 원인 봐줘")
    assert reply.route == "investigate"
    assert [c.name for c in runner.calls] == ["investigator", "evaluator", "reviser"]
    inv_spec = runner.calls[0]
    assert set(inv_spec.tools) == set(READ_TOOLS) and set(inv_spec.agents) == set(prompts.SCOUTS)
    assert not any(t.startswith("runbook") for t in inv_spec.tools)
    # reviser said high, evaluator capped at medium
    assert reply.meta["confidence"] == "medium"
    assert "EV-99" not in reply.meta["evidence"]  # gate removed the made-up citation...
    assert "인용 제거: EV-99" in reply.text  # ...and says so (transparency)
    assert "독립 검토자" in reply.text and "배제한 것" in reply.text
    notes = pipeline.notes.load("p1")
    assert notes["hypotheses"][-1]["confidence"] == "medium"
    assert notes["ruled_out"][0]["item"] == "보안그룹 변경"


async def test_evaluator_failure_limits_confidence(pipeline, runner):
    runner.on("investigator", investigator_fake())

    async def broken(spec, ctx, tools):
        return RunResult(name=spec.name, is_error=True, error="timeout")

    runner.on("evaluator", broken)
    reply = await pipeline.handle(thread_id="p2", user_id=REQUESTER, text="5xx 원인 봐줘")
    assert reply.meta["confidence"] == "low" and "검토 실패" in reply.text


async def test_investigator_error_is_reported(pipeline, runner):
    async def broken(spec, ctx, tools):
        return RunResult(name=spec.name, is_error=True, error="AccessDeniedException: bedrock:InvokeModel")

    runner.on("investigator", broken)
    reply = await pipeline.handle(thread_id="p3", user_id=REQUESTER, text="5xx 원인 봐줘")
    assert reply.route == "investigate" and "AccessDenied" in reply.text


def test_gate_rca_drops_unknown_ids_and_downgrades():
    rca, issues = gate_rca(RCA, {"EV-01", "EV-03"})
    assert [e["id"] for e in rca["evidence"]] == ["EV-01"]
    assert rca["ruled_out"][0]["evidence_ids"] == ["EV-03"]
    assert issues
    empty, _ = gate_rca(dict(RCA, evidence=[{"id": "EV-50", "fact": "x"}]), {"EV-01"})
    assert empty["confidence"] == "low"


def change_fake(propose=True, claim=None, instance=I1):
    async def fn(spec, ctx, tools):
        assert "runbook_propose" in spec.tools and "cost_daily_delta" not in spec.tools
        ev = await call_tool(tools, "logs_error_summary", {"service": "checkout-api"})
        approval_id = None
        if propose:
            out = await call_tool(tools, "runbook_propose", {
                "runbook_id": "restart-ec2-instance", "params": {"service": "checkout-api", "InstanceId": instance},
                "reason": "i-...0001에 에러 97% 집중, 커넥션 누수로 보여 재시작으로 완화", "evidence_ids": [ev["evidence_id"]]})
            approval_id = out.get("approval_id")
        return RunResult(name=spec.name, cost_usd=0.03, structured={
            "decision": "proposed" if (propose or claim) else "declined", "approval_id": claim or approval_id,
            "message": "재시작은 완화책입니다. 근본 조치는 롤백입니다."})
    return fn


async def test_change_request_produces_card_from_db_record(pipeline, runner):
    runner.on("change", change_fake())
    reply = await pipeline.handle(thread_id="p4", user_id=REQUESTER, text="재시작해줘", channel_id="C1")
    assert reply.route == "change_request" and reply.approval_id
    rec = pipeline.approvals.store.get(reply.approval_id)
    assert rec.status == "PENDING"
    actions = [b for b in reply.blocks if b["type"] == "actions"][0]["elements"]
    assert {a["action_id"] for a in actions} == {APPROVE_ACTION, REJECT_ACTION}
    assert all(a["value"] == rec.id for a in actions)  # buttons carry only the id
    assert "AWS-RestartEC2Instance" in reply.text and I1 in reply.text


async def test_model_claimed_approval_id_without_record_is_ignored(pipeline, runner):
    runner.on("change", change_fake(propose=False, claim="apr-deadbeef00"))
    reply = await pipeline.handle(thread_id="p5", user_id=REQUESTER, text="재시작해줘")
    assert reply.approval_id is None
    assert not any(b["type"] == "actions" for b in reply.blocks)
    assert any(r["event"] == "gate_violation" for r in pipeline.audit.read())


async def test_change_with_bad_target_gets_no_card(pipeline, runner):
    runner.on("change", change_fake(instance="i-0ffffffffffffffff"))
    reply = await pipeline.handle(thread_id="p6", user_id=REQUESTER, text="재시작해줘")
    assert reply.approval_id is None
    assert any(r["event"] == "proposal_rejected" and r["code"] == "not_in_inventory" for r in pipeline.audit.read())


async def test_policy_forbid_never_calls_model(pipeline, runner):
    reply = await pipeline.handle(thread_id="p7", user_id=REQUESTER, text="보안그룹 0.0.0.0/0 전체 개방해줘")
    assert reply.route == "policy_forbid" and runner.calls == []
    assert any(r["event"] == "policy_forbid" for r in pipeline.audit.read())


async def test_one_request_per_thread_at_a_time(pipeline, runner):
    gate = asyncio.Event()

    async def slow(spec, ctx, tools):
        await gate.wait()
        return RunResult(name=spec.name, is_error=True, error="stop")

    runner.on("investigator", slow)
    first = asyncio.create_task(pipeline.handle(thread_id="p8", user_id=REQUESTER, text="5xx 원인 봐줘"))
    await asyncio.sleep(0.05)
    second = await pipeline.handle(thread_id="p8", user_id=REQUESTER, text="5xx 원인 봐줘")
    assert second.route == "busy"
    gate.set()
    await first
