import json

import pytest

from aiops.config import PROJECT_ROOT
from aiops.runbooks import RunbookRegistry, load_runbook


def test_registry_allowlist_excludes_drafts():
    reg = RunbookRegistry(PROJECT_ROOT / "runbooks")
    assert reg.allowed_documents == frozenset({"AWS-RestartEC2Instance"})
    assert reg.get("rollback-codedeploy-deployment").executable is False
    rb = reg.get("restart-ec2-instance")
    assert rb.verify.metric == "5xx_rate" and rb.verify.max_value == 1.0
    assert reg.ssm_parameters(rb, {"service": "checkout-api", "InstanceId": "i-0a1b2c3d4e5f60001"}, None) == \
        {"InstanceId": ["i-0a1b2c3d4e5f60001"]}


def test_bad_runbook_status_rejected(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("---\nid: x\ntitle: x\nstatus: yolo\nssm_document: X\n---\nbody")
    with pytest.raises(ValueError):
        load_runbook(p)


async def test_cli_offline_cost_report(tmp_path, monkeypatch, capsys):
    from aiops import cli

    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("AIOPS_MODE", "mock")
    monkeypatch.setenv("AIOPS_WORKFLOW_LLM", "0")
    assert await cli.main_async(["어제 대비 비용이 튄 서비스는?", "--thread", "t-cli"]) == 0
    out = capsys.readouterr().out
    assert "EC2 - Other" in out and "+$155.30" in out
    assert await cli.main_async(["approve", "apr-nope", "--as", "U0X"]) == 2


async def test_agentcore_http_contract(tmp_path, monkeypatch):
    httpx = pytest.importorskip("httpx")
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("AIOPS_MODE", "mock")
    monkeypatch.setenv("AIOPS_WORKFLOW_LLM", "0")
    from aiops import agentcore_app

    agentcore_app._pipeline = None
    transport = httpx.ASGITransport(app=agentcore_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/ping")).json() == {"status": "Healthy"}
        r = await client.post("/invocations", json={"prompt": "알람 현황"},
                              headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "sess-1"})
        body = r.json()
        assert r.status_code == 200 and body["route"] == "alarm_report" and "ALARM" in body["response"]
        assert (await client.post("/invocations", content=b"not json")).status_code == 400
    assert json.loads((tmp_path / "var" / "threads" / "sess-1" / "notes.json").read_text())["reports"]
    agentcore_app._pipeline = None
