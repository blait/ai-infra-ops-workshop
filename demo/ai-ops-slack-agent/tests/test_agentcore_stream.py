import json

import pytest

httpx = pytest.importorskip("httpx")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("AIOPS_MODE", "mock")
    monkeypatch.setenv("AIOPS_WORKFLOW_LLM", "0")
    for k in ("AIOPS_GATEWAY_URL", "AIOPS_MEMORY_ID"):
        monkeypatch.delenv(k, raising=False)
    from aiops import agentcore_app

    agentcore_app._pipeline = None
    yield agentcore_app
    agentcore_app._pipeline = None


async def test_stream_mode_emits_sse_result(app):
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/invocations", json={"prompt": "알람 현황", "stream": True, "user_id": "U1"},
                              headers={app.SESSION_HEADER: "slack-thread-C1-1760500000_000100-xxxxxxxx"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    assert events and events[-1]["type"] == "result"
    assert events[-1]["status"] == "success" and events[-1]["route"] == "alarm_report"
    assert "ALARM" in events[-1]["response"]


async def test_json_mode_includes_meta(app):
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/invocations", json={"prompt": "어제 대비 비용이 튄 서비스는?"},
                              headers={app.SESSION_HEADER: "sess-json"})
        body = r.json()
        assert body["status"] == "success" and body["route"] == "cost_report"
        assert body["meta"]["route_rule"] == "R-COST" and body["meta"]["evidence_created"]
        assert body["meta"]["gateway_calls"] == [] and body["approval_id"] is None
        bad = await client.post("/invocations", json={"prompt": "  "})
        assert bad.status_code == 400


def test_build_pipeline_local_mode_has_no_remote(app):
    p = app.get_pipeline()
    assert p.remote is None and p.memory is None and p.approvals is not None


def test_build_pipeline_deployed_mode_wiring(app, monkeypatch):
    monkeypatch.setenv("AIOPS_GATEWAY_URL", "https://gw.example.com")
    monkeypatch.setenv("AIOPS_GATEWAY_TARGETS", "readtools:cost_daily_delta;changeproposal:runbook_propose")
    monkeypatch.setenv("AIOPS_MEMORY_ID", "aiops_notes-abcdefghij")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    p = app.build_pipeline()
    assert p.approvals is None and p.executor is None
    assert p.remote.url == "https://gw.example.com/mcp"
    assert p.remote.gateway_tool_name("runbook_propose") == "changeproposal___runbook_propose"
    assert p.memory.memory.memory_id == "aiops_notes-abcdefghij"
