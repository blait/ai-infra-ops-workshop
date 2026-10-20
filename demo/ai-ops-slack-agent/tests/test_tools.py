import json
from collections import defaultdict

import pytest

from aiops.tools import READ_TOOLS, RunContext, build_server, build_tools
from aiops.tools.summarize import fit_json, redact, signature

from .conftest import I1, REQUESTER, call_tool


@pytest.fixture
def ctx():
    return RunContext(thread_id="t-tools", user_id=REQUESTER, route="investigate")


@pytest.fixture
def tools(pipeline, ctx):
    return build_tools(ctx, pipeline.deps)


READ_CALLS = [
    ("cloudwatch_alarm_status", {}),
    ("cloudwatch_metric_summary", {"service": "checkout-api", "metric": "5xx_rate"}),
    ("logs_error_summary", {"service": "checkout-api"}),
    ("cloudtrail_recent_changes", {"service": "checkout-api"}),
    ("cost_daily_delta", {}),
]


@pytest.mark.parametrize("name,args", READ_CALLS)
async def test_read_tools_return_capped_summaries_with_evidence(pipeline, ctx, tools, name, args):
    res = await tools[name].handler(args)
    text = res["content"][0]["text"]
    assert not res.get("is_error")
    assert len(text) <= pipeline.settings.tool_output_max_chars
    body = json.loads(text)
    assert body["evidence_id"].startswith("EV-") and body["summary"]
    # evidence is persisted with exactly what the model saw
    stored = pipeline.notes.get_evidence(ctx.thread_id, body["evidence_id"])
    assert stored["payload"] == text
    assert body["evidence_id"] in pipeline.notes.evidence_ids(ctx.thread_id)


async def test_small_cap_truncates_lists_but_keeps_valid_json(pipeline, ctx):
    pipeline.deps.max_chars = 900
    tools = build_tools(ctx, pipeline.deps)
    res = await tools["logs_error_summary"].handler({"service": "checkout-api"})
    text = res["content"][0]["text"]
    assert len(text) <= 900
    body = json.loads(text)
    assert body["_truncated"] is True and body["evidence_id"]


async def test_logs_summary_is_aggregated_redacted_and_marks_untrusted(pipeline, tools):
    body = await call_tool(tools, "logs_error_summary", {"service": "checkout-api", "minutes": 180})
    data = body["data"]
    raw_rows = json.loads((pipeline.settings.fixtures_dir / "logs.json").read_text())
    assert data["matched_total"] == len(raw_rows)  # exact count, not just the sample
    assert data["sample_size"] <= pipeline.settings.log_row_cap
    assert next(iter(data["by_instance"])) == I1
    assert data["versions_seen_by_instance"]["i-0a1b2c3d4e5f60002"] == ["v2.13.2"]
    text = json.dumps(body, ensure_ascii=False)
    assert "kim.minji@example.com" not in text and "eyJhbGciOi" not in text
    assert "untrusted" in data["untrusted_data_note"] or "외부 입력" in data["untrusted_data_note"]
    assert len(data["signatures"]) <= 8


async def test_metric_summary_change_point_and_actionable_error(tools):
    ok = await call_tool(tools, "cloudwatch_metric_summary", {"service": "checkout-api", "metric": "5xx_rate"})
    assert ok["data"]["change_point"] == "13:45"
    assert ok["data"]["baseline_median"] < 0.3 and ok["data"]["current"] > 10
    assert len(ok["data"]["series"]) <= 14
    bad = await call_tool(tools, "cloudwatch_metric_summary", {"service": "checkout-api", "metric": "bogus"})
    assert bad["_is_error"] and "5xx_rate" in bad["error"]
    unknown = await call_tool(tools, "cloudwatch_metric_summary", {"service": "nope", "metric": "cpu"})
    assert unknown["_is_error"] and "checkout-api" in unknown["error"]


async def test_cloudtrail_filters_reads_and_flags_related(tools):
    body = await call_tool(tools, "cloudtrail_recent_changes", {"service": "checkout-api", "hours": 6})
    events = body["data"]["events"]
    names = [e["event"] for e in events]
    assert "DescribeInstances" not in names and "GetParameter" not in names
    related = {e["event"]: e for e in events if e["related_to"]}
    assert set(related) == {"CreateDeployment", "AuthorizeSecurityGroupIngress"}
    assert related["CreateDeployment"]["at"] == "13:42"


async def test_cost_delta_matches_independent_recomputation(pipeline, tools):
    body = await call_tool(tools, "cost_daily_delta", {})
    raw = json.loads((pipeline.settings.fixtures_dir / "cost.json").read_text())
    by_day = defaultdict(float)
    for r in raw:
        if not r["estimated"]:
            by_day[r["date"]] += r["amount"]
    days = sorted(by_day)
    assert body["data"]["compare"] == {"yesterday": days[-1], "day_before": days[-2],
                                       "avg_window": body["data"]["compare"]["avg_window"]}
    assert body["data"]["total"]["delta"] == pytest.approx(by_day[days[-1]] - by_day[days[-2]], abs=0.011)
    top = body["data"]["top_movers"][0]
    raw_other = {r["date"]: r["amount"] for r in raw if r["service"] == "EC2 - Other"}
    assert top["service"] == "EC2 - Other"
    assert top["delta"] == pytest.approx(raw_other[days[-1]] - raw_other[days[-2]], abs=0.001)
    assert body["data"]["partial_today"]["date"] == "2026-10-15"  # estimated day excluded from comparison


async def test_evidence_get_unknown_id_lists_known(tools):
    await call_tool(tools, "cloudwatch_alarm_status", {})
    res = await call_tool(tools, "evidence_get", {"evidence_id": "EV-99"})
    assert res["_is_error"] and "EV-01" in res["error"]


async def test_runbook_tools_progressive_disclosure(tools):
    listed = await call_tool(tools, "runbook_list", {})
    assert {r["id"] for r in listed["runbooks"]} == {"restart-ec2-instance", "rollback-codedeploy-deployment"}
    assert all("body" not in r for r in listed["runbooks"])  # list = one-liners only
    full = await call_tool(tools, "runbook_get", {"runbook_id": "restart-ec2-instance"})
    assert "완료 조건" in full["body"]


async def test_propose_tool_only_creates_pending_record(pipeline, ctx, tools):
    ev = await call_tool(tools, "logs_error_summary", {"service": "checkout-api"})
    out = await call_tool(tools, "runbook_propose", {
        "runbook_id": "restart-ec2-instance", "params": {"service": "checkout-api", "InstanceId": I1},
        "reason": "i-...0001에만 커넥션 누수와 풀 고갈 에러가 몰림", "evidence_ids": [ev["evidence_id"]]})
    assert out["status"] == "PENDING_APPROVAL" and "실행되지 않았다" in out["note"]
    rec = pipeline.approvals.store.get(out["approval_id"])
    assert rec.status == "PENDING" and rec.execution_id is None
    assert ctx.proposals == [rec.id]


async def test_build_server_exposes_only_route_tools(pipeline, ctx):
    server = build_server(ctx, pipeline.deps, list(READ_TOOLS))
    assert server["type"] == "sdk" and server["name"] == "aiops"
    with pytest.raises(ValueError):
        build_server(ctx, pipeline.deps, ["cloudwatch_alarm_status", "execute_anything"])


def test_fit_json_and_redaction_helpers():
    big = {"a": list(range(2000)), "b": "x"}
    out = fit_json(big, 300)
    assert len(out) <= 300 and json.loads(out)["_truncated"]
    fake_key = "AKIA" + "ABCDEFGHIJKLMNOP"  # built at runtime so secret scanners do not flag the test file
    assert redact(f"user a.b@example.com Bearer abcdefghijkl {fake_key}") == "user <email> Bearer <token> <aws-key>"
    assert signature("[v2.14.0] ERROR x waiting=187 thread http-nio-8080-exec-41 ORD-1234567") == \
        "ERROR x waiting=<n> thread http-nio-<n>-exec=<n> ORD-<n>"


async def test_spike_bin_is_reported_as_a_range(tools):
    """Live finding (2026-10-18): a bare bin start "13:40" was read as 'before the 13:42 deploy'."""
    body = await call_tool(tools, "logs_error_summary", {"service": "checkout-api", "minutes": 180})
    assert body["data"]["first_spike_bin"] == "13:40~13:45 (5분 구간)"
