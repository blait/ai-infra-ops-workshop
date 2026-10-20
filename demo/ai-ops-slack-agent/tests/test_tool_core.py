import pytest

from aiops.approvals import ApprovalService, ApprovalStore
from aiops.memory import MemoryActionSink
from aiops.tools.core import ALL_TOOLS, Caller, ToolCore, metric_hint, tool_specs

from .conftest import I1, REQUESTER

READ_CALLS = [
    ("cloudwatch_alarm_status", {"state": "ANY"}),
    ("cloudwatch_metric_summary", {"service": "checkout-api", "metric": "5xx_rate"}),
    ("logs_error_summary", {"service": "checkout-api"}),
    ("cloudtrail_recent_changes", {"service": "checkout-api"}),
    ("cost_daily_delta", {}),
]


@pytest.fixture
def core(pipeline):
    return ToolCore(pipeline.backend, pipeline.runbooks, None)


CALLER = Caller(thread_id="t-core", user_id=REQUESTER, channel_id="C1")


@pytest.mark.parametrize("name,args", READ_CALLS)
async def test_read_tools_return_evidence_envelopes(core, name, args):
    env = await core.call(name, args, CALLER)
    assert env["ok"] and env["kind"] == "evidence" and env["tool"] == name and env["summary"]
    assert isinstance(env["data"], dict)


async def test_alarm_items_carry_iso_since(core):
    env = await core.call("cloudwatch_alarm_status", {"state": "ANY"}, CALLER)
    a = env["data"]["alarms"][0]
    assert a["since_iso"].endswith("Z") and isinstance(a["for_minutes"], int)


async def test_unknown_and_local_only_tools_refused(core):
    assert (await core.call("execute_anything", {}, CALLER))["code"] == "unknown_tool"
    assert (await core.call("evidence_get", {"evidence_id": "EV-01"}, CALLER))["code"] == "unknown_tool"


async def test_bad_service_is_actionable_error(core):
    env = await core.call("logs_error_summary", {"service": "nope"}, CALLER)
    assert env["ok"] is False and "checkout-api" in env["error"]


async def test_runbook_tools(core):
    lst = await core.call("runbook_list", {}, CALLER)
    assert lst["kind"] == "json" and {r["id"] for r in lst["body"]["runbooks"]} >= {"restart-ec2-instance"}
    got = await core.call("runbook_get", {"runbook_id": "restart-ec2-instance"}, CALLER)
    assert "완료 조건" in got["body"]["body"]
    assert (await core.call("runbook_get", {"runbook_id": "x"}, CALLER))["code"] == "unknown_runbook"


async def test_propose_without_approvals_or_caller(pipeline, core):
    assert (await core.call("runbook_propose", {}, CALLER))["code"] == "not_configured"
    with_svc = ToolCore(pipeline.backend, pipeline.runbooks, pipeline.approvals)
    env = await with_svc.call("runbook_propose", {}, Caller(thread_id="", user_id=""))
    assert env["code"] == "no_caller"


async def test_propose_uses_caller_known_evidence(pipeline, settings, tmp_path):
    # Lambda-like service: notes sink knows no evidence; trusted caller context supplies the ids.
    svc = ApprovalService(store=ApprovalStore(tmp_path / "a.sqlite3"), runbooks=pipeline.runbooks,
                          backend=pipeline.backend, executor=None, audit=pipeline.audit,
                          notes=MemoryActionSink(None), approvers=frozenset())
    core = ToolCore(pipeline.backend, pipeline.runbooks, svc)
    args = {"runbook_id": "restart-ec2-instance", "params": {"service": "checkout-api", "InstanceId": I1},
            "reason": "i-...0001에만 커넥션 누수 에러가 몰림", "evidence_ids": ["EV-01"]}
    denied = await core.call("runbook_propose", args, Caller(thread_id="t1", user_id=REQUESTER))
    assert denied["ok"] is False and denied["code"] == "unknown_evidence"
    ok = await core.call("runbook_propose", args, Caller(thread_id="t1", user_id=REQUESTER,
                                                         known_evidence_ids=["EV-01"]))
    assert ok["ok"] and ok["approval_id"].startswith("apr-")
    assert ok["record"]["status"] == "PENDING" and ok["record"]["id"] == ok["approval_id"]
    assert ok["body"]["status"] == "PENDING_APPROVAL"


def test_caller_round_trip():
    c = Caller(thread_id="t", user_id="U1", channel_id="C1", known_evidence_ids=["EV-01", "EV-02"])
    assert Caller.from_json(c.to_json()) == c
    empty = Caller.from_json(None)
    assert empty.thread_id == "" and empty.channel_id is None and empty.known_evidence_ids == []


def test_tool_specs_cover_all_tools(pipeline):
    services = pipeline.backend.services()
    specs = tool_specs(sorted(services), metric_hint(services))
    assert set(specs) == set(ALL_TOOLS) and len(specs) == 9
    for desc, schema in specs.values():
        assert desc and schema["type"] == "object" and "properties" in schema
    assert "5xx_rate" in metric_hint(services)
