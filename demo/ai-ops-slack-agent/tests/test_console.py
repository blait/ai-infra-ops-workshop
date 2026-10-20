import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aiops.approvals import ApprovalError, ApprovalStore
from aiops.console import AwsConsole, CHANNEL, consume_stream, display_text, new_session
from aiops.aws_toolkit import ManagedAwsMcpClient
from .test_lambda_handlers import FakeStore, Recorder


class Stream:
    def __init__(self, events):
        self.events, self.closed = events, False

    def iter_lines(self):
        yield b": keepalive"
        for event in self.events:
            yield b"data: " + json.dumps(event).encode()

    def close(self):
        self.closed = True


def test_stream_requires_final_success_and_closes_on_failure():
    progress = []
    stream = Stream([{"type": "progress", "message": "collecting"}])
    with pytest.raises(RuntimeError, match="결과 없이"):
        consume_stream(stream, progress.append)
    assert progress == ["collecting"] and stream.closed
    stream = Stream([{"type": "result", "status": "error", "error": "denied"}])
    with pytest.raises(RuntimeError, match="denied"):
        consume_stream(stream, progress.append)
    assert stream.closed


def test_stream_result_and_account_masking():
    stream = Stream([{"type": "result", "status": "success", "response": "done"}])
    assert consume_stream(stream, lambda _: None)["response"] == "done"
    assert stream.closed
    text = display_text("arn:aws:sts::123456789012:assumed-role/demo/user 123456789012계정 10.2.3.4 i-0123456789abcdef0가")
    assert "123456789012" not in text and "10.2.3.4" not in text and "0123456789abcdef0" not in text


@pytest.fixture
def console(tmp_path, monkeypatch):
    import aiops.console as module
    monkeypatch.setattr(module, "ROOT", tmp_path)
    c = AwsConsole.__new__(AwsConsole)
    c.sid = new_session()
    c.store = FakeStore(tmp_path / "approvals.db")
    now = datetime.now(timezone.utc)
    c.store.insert({
        "id": "apr-console", "thread_id": c.sid, "channel_id": CHANNEL, "card_ts": None,
        "requested_by": "aws-operator", "runbook_id": "restart-ec2-instance",
        "document": "AWS-RestartEC2Instance", "document_version": "$DEFAULT",
        "params_json": json.dumps({"InstanceId": "i-0123456789abcdef0"}), "target_hash": "test-target",
        "reason": "test proposal", "evidence_json": '["EV-01"]', "status": "PENDING",
        "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=30)).isoformat(),
    })
    c.store.set_fields("apr-console", task_token="never-send-this-to-browser", sfn_execution_arn="flow")
    c.sfn = Recorder(describe_execution={"status": "RUNNING"})
    c.client = lambda service, **kw: c.sfn
    c.identity = lambda: "arn:aws:sts::123456789012:assumed-role/operator/user"
    c.memory = SimpleNamespace(put_blob=lambda *args, **kwargs: None)
    return c


def test_operator_approval_is_conditional_and_exposes_no_task_token(console):
    c = console
    result = c.decide("apr-console", c.sid, "approve")
    assert result["status"] == "APPROVED" and result["decided_by"] == c.identity()
    assert "task_token" not in result and "never-send-this" not in json.dumps(result)
    assert len(c.sfn.named("send_task_success")) == 1
    with pytest.raises(ApprovalError, match="이미 처리"):
        c.decide("apr-console", c.sid, "approve")
    assert len(c.sfn.named("send_task_success")) == 1


def test_wrong_session_or_slack_context_cannot_decide(console):
    with pytest.raises(ApprovalError, match="현재 Streamlit"):
        console.decide("apr-console", new_session(), "approve")
    console.store.set_fields("apr-console", channel_id="C-SLACK")
    with pytest.raises(ApprovalError, match="현재 Streamlit"):
        console.decide("apr-console", console.sid, "approve")
    assert console.store.get("apr-console").status == "PENDING"
    assert console.sfn.calls == []


def test_expired_approval_cannot_resume_execution(console):
    ApprovalStore.set_fields(console.store, "apr-console",
                             expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    with pytest.raises(ApprovalError, match="승인 대기 시간"):
        console.decide("apr-console", console.sid, "approve")
    assert console.store.get("apr-console").status == "EXPIRED"
    assert console.sfn.named("send_task_success") == []


def test_callback_failure_is_visible_and_retry_does_not_decide_again(console):
    console.sfn.fail.add("send_task_success")
    with pytest.raises(RuntimeError, match="결정은 저장"):
        console.decide("apr-console", console.sid, "reject")
    assert console.store.get("apr-console").status == "REJECTED"
    console.sfn.fail.clear()
    assert console.resume_decision("apr-console", console.sid)["status"] == "REJECTED"
    console.identity = lambda: "some-other-operator"
    with pytest.raises(ApprovalError, match="본인이"):
        console.resume_decision("apr-console", console.sid)


def test_live_and_scenario_sessions_cannot_mix(console):
    with pytest.raises(ValueError, match="일치하지"):
        console.invoke("question", console.sid, "aws_live", lambda _: None)


def test_managed_mcp_keeps_api_call_errors_and_does_not_execute_python_locally(monkeypatch):
    client = ManagedAwsMcpClient()
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        return {"result": {"content": [{"type": "text", "text": json.dumps({
            "api_calls": [{"operation": "DescribeVpcs", "error": "AccessDenied"}],
            "return_value": None})}]}}
    monkeypatch.setattr(client, "rpc", rpc)
    result = client.read("result = 1\nresult")
    assert result["data"]["result"]["api_calls"][0]["error"] == "AccessDenied"
    assert calls[0][1]["name"] == "aws___run_script"
    assert not client.read("")["ok"]
    monkeypatch.setattr(client, "rpc", lambda *args: {"error": {"message": "AccessDenied"}})
    assert not client.read("result")["ok"]


def test_status_uses_actual_gateway_policy_arn_field():
    c = AwsConsole.__new__(AwsConsole)
    c.region, c.stack_status = "us-east-1", "UPDATE_COMPLETE"
    c.outputs = {"RuntimeId": "runtime", "GatewayId": "gateway", "MemoryId": "memory", "PolicyEngineId": "policy"}
    control = Recorder(
        get_agent_runtime={"status": "READY", "agentRuntimeVersion": "9", "environmentVariables": {
            "AIOPS_MEMORY_ID": "memory", "AIOPS_GATEWAY_TARGETS": "awstoolkit:aws_mcp_read"}},
        get_gateway={"status": "READY", "authorizerType": "AWS_IAM",
                     "policyEngineConfiguration": {"arn": "arn:aws:policy", "mode": "ENFORCE"}},
        get_memory={"memory": {"status": "ACTIVE", "eventExpiryDuration": 30}},
        list_gateway_targets={"items": [{"name": "awstoolkit", "status": "READY"}]},
    )
    c.client = lambda service: control
    assert c.status()["ready"]
    control.returns["get_gateway"]["policyEngineConfiguration"]["mode"] = "LOG_ONLY"
    assert not c.status()["ready"]


def test_managed_mcp_initializes_sends_session_header_and_closes():
    import boto3

    seen = []
    class Response:
        def __init__(self, data, headers=None):
            self.data, self.headers = data, headers or {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps(self.data).encode()
    def opener(request, timeout):
        payload = json.loads(request.data) if request.data else {}
        seen.append(payload.get("method", request.method))
        if payload.get("method") == "initialize":
            return Response({"result": {"protocolVersion": "2025-03-26"}}, {"Mcp-Session-Id": "session-from-server"})
        headers = {k.lower(): v for k, v in request.header_items()}
        assert headers["mcp-session-id"] == "session-from-server"
        assert headers["mcp-protocol-version"] == "2025-03-26"
        if payload.get("method") == "tools/call":
            return Response({"result": {"structuredContent": {"api_calls": [{"ok": True}], "return_value": 1}}})
        return Response({})
    client = ManagedAwsMcpClient(
        session=boto3.Session(aws_access_key_id="test", aws_secret_access_key="test"), opener=opener)
    assert client.read("result=1\nresult")["ok"]
    assert seen == ["initialize", "notifications/initialized", "tools/call", "DELETE"]
    assert client.mcp_session_id is None
