"""SigV4-signed MCP client for Amazon Bedrock AgentCore Gateway (inbound auth = AWS_IAM).

The agent runtime role has `bedrock-agentcore:InvokeGateway` on this one gateway and nothing else
that touches AWS data. Each tool call is a JSON-RPC `tools/call` to `<gateway>/mcp` with the tool name
`<target>___<tool>`. Before the Lambda target runs, the Gateway asks the attached policy engine
(Cedar, ENFORCE): the principal is `AgentCore::IamEntity::"arn:aws:sts::<acct>:assumed-role/<role>"`,
the action is `AgentCore::Action::"<target>___<tool>"`. A denied call never reaches the Lambda.

Why not let Claude Code connect to the Gateway as a remote MCP server directly: Claude Code's HTTP
MCP client cannot SigV4-sign, and the in-process wrapper must stay in the loop to register evidence
ids, cap result size, audit, and inject the trusted `caller` context into runbook_propose.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
import urllib.error
import urllib.request
from typing import Any

SIGNING_SERVICE = "bedrock-agentcore"
TOOL_DELIMITER = "___"


def parse_targets(spec: str | None) -> dict[str, str]:
    """'readtools:a|b;changeproposal:c' -> {'a': 'readtools', 'b': 'readtools', 'c': 'changeproposal'}"""
    out: dict[str, str] = {}
    for part in (spec or "").split(";"):
        if not part.strip():
            continue
        target, _, tools = part.partition(":")
        for t in tools.split("|"):
            if t.strip():
                out[t.strip()] = target.strip()
    return out


def _parse_body(raw: bytes, content_type: str) -> dict:
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in (content_type or ""):
        last = None
        for line in text.splitlines():
            if line.startswith("data:"):
                last = line[5:].strip()
        text = last or "{}"
    return json.loads(text or "{}")


def classify_error(message: str) -> str:
    m = (message or "").lower()
    if any(k in m for k in ("policy", "not authorized", "unauthorized", "denied", "forbidden", "access")):
        return "denied"
    return "error"


class GatewayToolClient:
    def __init__(self, url: str, targets: dict[str, str], region: str, session=None, timeout: float = 90.0,
                 opener=None):
        if not url.rstrip("/").endswith("/mcp"):
            url = url.rstrip("/") + "/mcp"
        self.url = url
        self.targets = targets
        self.region = region
        self.timeout = timeout
        self._session = session
        self._opener = opener or urllib.request.urlopen
        self._ids = itertools.count(1)

    def gateway_tool_name(self, tool: str) -> str:
        target = self.targets.get(tool)
        if not target:
            raise KeyError(f"tool {tool!r} is not mapped to a gateway target")
        return f"{target}{TOOL_DELIMITER}{tool}"

    # ------------------------------------------------------------------ transport
    def _signed_request(self, payload: dict) -> urllib.request.Request:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        if self._session is None:
            import boto3

            self._session = boto3.Session(region_name=self.region)
        creds = self._session.get_credentials().get_frozen_credentials()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = AWSRequest(method="POST", url=self.url, data=body,
                         headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        SigV4Auth(creds, SIGNING_SERVICE, self.region).add_auth(req)
        return urllib.request.Request(self.url, data=body, headers=dict(req.headers.items()), method="POST")

    def rpc(self, method: str, params: dict | None = None) -> dict:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}}
        req = self._signed_request(payload)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                return _parse_body(resp.read(), resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            try:
                body = _parse_body(raw, e.headers.get("Content-Type", "") if e.headers else "")
            except (ValueError, json.JSONDecodeError):
                body = {"error": {"code": e.code, "message": raw.decode("utf-8", errors="replace")[:500]}}
            if not body.get("error"):
                body["error"] = {"code": e.code, "message": body.get("message") or body.get("Message") or f"HTTP {e.code}"}
            if e.code in (401, 403) and isinstance(body["error"], dict):
                body["error"]["message"] = f"access denied (HTTP {e.code}): {body['error'].get('message')}"
            body["_http_status"] = e.code
            return body

    # ------------------------------------------------------------------ API
    def list_tools(self) -> list[str]:
        names, cursor = [], None
        while True:
            res = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            result = res.get("result") or {}
            names += [t.get("name") for t in result.get("tools", [])]
            cursor = result.get("nextCursor")
            if not cursor:
                return names

    def call_sync(self, tool: str, args: dict) -> tuple[dict, dict]:
        name = self.gateway_tool_name(tool)
        started = time.monotonic()
        res = self.rpc("tools/call", {"name": name, "arguments": args})
        info: dict[str, Any] = {"tool": tool, "gateway_tool": name, "ms": int((time.monotonic() - started) * 1000)}
        if "error" in res and res["error"]:
            err = res["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            info.update(outcome=classify_error(msg), error=str(msg)[:300], http_status=res.get("_http_status"))
            code = "policy_denied" if info["outcome"] == "denied" else "gateway_error"
            return {"ok": False, "code": code, "error": f"[{code}] Gateway가 호출을 거부/실패했습니다: {msg}"}, info
        result = res.get("result") or {}
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        text = "".join(texts)
        envelope: dict | None = None
        try:
            parsed = json.loads(text) if text else None
            if isinstance(parsed, dict) and "ok" in parsed:
                envelope = parsed
        except json.JSONDecodeError:
            pass
        if result.get("isError"):
            outcome = classify_error(text)
            info.update(outcome=outcome, error=text[:300])
            if envelope is None:
                code = "policy_denied" if outcome == "denied" else "gateway_error"
                envelope = {"ok": False, "code": code, "error": f"[{code}] {text[:500]}"}
            return envelope, info
        if envelope is None:
            info.update(outcome="error", error="non-envelope response")
            return {"ok": False, "code": "bad_response", "error": f"Gateway 응답 형식 오류: {text[:300]}"}, info
        info["outcome"] = "ok" if envelope.get("ok") else "tool_error"
        return envelope, info

    async def call(self, tool: str, args: dict) -> tuple[dict, dict]:
        return await asyncio.to_thread(self.call_sync, tool, args)
