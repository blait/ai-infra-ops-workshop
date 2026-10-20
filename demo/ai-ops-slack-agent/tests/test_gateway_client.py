import io
import json
import urllib.error

import pytest
from botocore.credentials import Credentials

from aiops.gateway_client import GatewayToolClient, classify_error, parse_targets

URL = "https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com"


class FakeSession:
    def get_credentials(self):
        return Credentials("AKIDEXAMPLE", "secret", "token")


class FakeResp:
    def __init__(self, body: bytes, ctype="application/json"):
        self._body = body
        self.headers = {"Content-Type": ctype}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def rpc_result(envelope=None, *, is_error=False, text=None):
    t = text if text is not None else json.dumps(envelope)
    return FakeResp(json.dumps({"jsonrpc": "2.0", "id": 1,
                                "result": {"isError": is_error, "content": [{"type": "text", "text": t}]}}).encode())


def client(opener):
    return GatewayToolClient(URL, parse_targets("readtools:cloudwatch_alarm_status|runbook_list;changeproposal:runbook_propose"),
                             region="us-east-1", session=FakeSession(), opener=opener)


def test_parse_targets_and_tool_name():
    t = parse_targets("readtools:a|b; changeproposal:c;")
    assert t == {"a": "readtools", "b": "readtools", "c": "changeproposal"}
    c = client(Opener([]))
    assert c.url.endswith("/mcp")
    assert c.gateway_tool_name("cloudwatch_alarm_status") == "readtools___cloudwatch_alarm_status"
    with pytest.raises(KeyError):
        c.gateway_tool_name("nope")


def test_request_is_sigv4_signed_post_to_mcp():
    op = Opener([rpc_result({"ok": True, "kind": "json", "body": {}})])
    env, info = client(op).call_sync("runbook_list", {})
    req = op.requests[0]
    assert req.get_method() == "POST" and req.full_url == URL + "/mcp"
    headers = {k.lower(): v for k, v in req.header_items()}
    assert "bedrock-agentcore" in headers["authorization"] and headers["authorization"].startswith("AWS4-HMAC-SHA256")
    assert "x-amz-date" in headers and headers["x-amz-security-token"] == "token"
    body = json.loads(req.data)
    assert body["method"] == "tools/call" and body["params"]["name"] == "readtools___runbook_list"
    assert env["ok"] and info["outcome"] == "ok" and info["gateway_tool"] == "readtools___runbook_list"


def test_success_envelope_parsed():
    envelope = {"ok": True, "kind": "evidence", "tool": "cloudwatch_alarm_status", "summary": "s", "data": {"x": 1}}
    env, info = client(Opener([rpc_result(envelope)])).call_sync("cloudwatch_alarm_status", {"state": "ANY"})
    assert env == envelope and info["outcome"] == "ok"


def test_tool_error_envelope_outcome():
    env, info = client(Opener([rpc_result({"ok": False, "error": "bad"})])).call_sync("runbook_list", {})
    assert env["ok"] is False and info["outcome"] == "tool_error"


def test_jsonrpc_policy_denied():
    resp = FakeResp(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
        "code": -32002, "message": "Tool call not authorized by policy"}}).encode())
    env, info = client(Opener([resp])).call_sync("runbook_propose", {})
    assert env["ok"] is False and env["code"] == "policy_denied"
    assert info["outcome"] == "denied"


def test_is_error_with_non_json_text():
    env, info = client(Opener([rpc_result(text="Lambda crashed", is_error=True)])).call_sync("runbook_list", {})
    assert env["ok"] is False and env["code"] == "gateway_error" and "Lambda crashed" in env["error"]
    assert info["outcome"] == "error"


def test_non_envelope_success_is_bad_response():
    env, _ = client(Opener([rpc_result(text="hello")])).call_sync("runbook_list", {})
    assert env["code"] == "bad_response"


def test_sse_body_parsed():
    inner = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [
        {"type": "text", "text": json.dumps({"ok": True, "kind": "json", "body": {"a": 1}})}]}})
    sse = f"event: message\ndata: {inner}\n\n".encode()
    env, _ = client(Opener([FakeResp(sse, "text/event-stream")])).call_sync("runbook_list", {})
    assert env["body"] == {"a": 1}


def _http_403():
    return urllib.error.HTTPError(URL + "/mcp", 403, "Forbidden", {"Content-Type": "application/json"},
                                  io.BytesIO(b'{"message": "AccessDenied"}'))


def test_http_error_path():
    env, info = client(Opener([_http_403()])).call_sync("runbook_list", {})
    assert env["ok"] is False and info["http_status"] == 403
    assert env["code"] in {"gateway_error", "policy_denied"}


def test_http_403_classified_as_denied():
    env, info = client(Opener([_http_403()])).call_sync("runbook_list", {})
    assert info["outcome"] == "denied" and "AccessDenied" in env["error"]


def test_list_tools_paginates():
    p1 = FakeResp(json.dumps({"result": {"tools": [{"name": "a"}], "nextCursor": "c1"}}).encode())
    p2 = FakeResp(json.dumps({"result": {"tools": [{"name": "b"}]}}).encode())
    op = Opener([p1, p2])
    assert client(op).list_tools() == ["a", "b"]
    assert json.loads(op.requests[1].data)["params"] == {"cursor": "c1"}


def test_classify_error():
    assert classify_error("Access denied") == "denied"
    assert classify_error("timeout talking to lambda") == "error"


async def test_async_call_wraps_sync():
    env, _ = await client(Opener([rpc_result({"ok": True, "kind": "json", "body": {}})])).call("runbook_list", {})
    assert env["ok"]
