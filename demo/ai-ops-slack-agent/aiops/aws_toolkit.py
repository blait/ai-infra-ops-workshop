"""Agent Toolkit for AWS, behind a Gateway Lambda with a read-only IAM allowlist.

Python executes in the managed AWS MCP sandbox, never on the UI host or Lambda.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .gateway_client import GatewayToolClient, _parse_body

TOOLKIT_TOOLS = ("aws_mcp_read",)
TOOLKIT_READ_ACTIONS = [
    "ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ec2:DescribeRegions",
    "cloudwatch:DescribeAlarms", "cloudwatch:GetMetricData", "cloudwatch:ListMetrics",
    "logs:DescribeLogGroups", "logs:FilterLogEvents",
    "cloudtrail:LookupEvents", "ce:GetCostAndUsage",
    "cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents",
    "cloudformation:ListStackResources",
]
TOOLKIT_SPEC = {
    "aws_mcp_read": (
        "실제 AWS 계정을 Agent Toolkit for AWS의 AWS MCP Server로 조회. "
        "sandbox에서 await call_boto3(service_name=..., operation_name='DescribeInstances', "
        "region_name=..., params={...}) 사용. operation_name은 PascalCase. "
        "마지막 줄은 result. 읽기 IAM만 있고 변경은 거부됨. 필요한 필드와 전체 건수로 요약.",
        {"type": "object", "properties": {
            "code": {"type": "string", "description": "AWS MCP sandbox Python. result={...}; 마지막 줄 result"}
        }, "required": ["code"], "additionalProperties": False},
    )
}


class ManagedAwsMcpClient(GatewayToolClient):
    def __init__(self, region="us-east-1", **kwargs):
        super().__init__(f"https://aws-mcp.{region}.api.aws/mcp", {}, region, **kwargs)
        self.mcp_session_id = None
        self.protocol_version = "2025-03-26"

    def _signed_request(self, payload, method="POST"):
        import boto3
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        session = self._session or boto3.Session(region_name=self.region)
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.protocol_version}
        if self.mcp_session_id:
            headers["Mcp-Session-Id"] = self.mcp_session_id
        request = AWSRequest(method=method, url=self.url, data=body, headers=headers)
        SigV4Auth(session.get_credentials().get_frozen_credentials(), "aws-mcp", self.region).add_auth(request)
        return urllib.request.Request(self.url, data=body, headers=dict(request.headers.items()), method=method)

    def _request(self, payload):
        try:
            with self._opener(self._signed_request(payload), timeout=self.timeout) as response:
                self.mcp_session_id = response.headers.get("Mcp-Session-Id") or self.mcp_session_id
                return _parse_body(response.read(), response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            return {"error": {"code": exc.code, "message": exc.read().decode(errors="replace")[:1200]}}

    def rpc(self, method, params=None):
        if not self.mcp_session_id:
            init = self._request({"jsonrpc": "2.0", "id": next(self._ids), "method": "initialize", "params": {
                "protocolVersion": self.protocol_version, "capabilities": {},
                "clientInfo": {"name": "aiops-agentcore-toolkit", "version": "1.0"}}})
            if init.get("error"):
                return init
            if not self.mcp_session_id:
                return {"error": {"message": "AWS MCP initialize did not return a session id"}}
            self.protocol_version = init.get("result", {}).get("protocolVersion", self.protocol_version)
            self._request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self._request({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}})

    def close(self):
        if self.mcp_session_id:
            try:
                with self._opener(self._signed_request(None, method="DELETE"), timeout=10):
                    pass
            except Exception:
                pass  # Remote expiration is also safe; closing must not replace the actual query result.
            finally:
                self.mcp_session_id = None

    def read(self, code: str) -> dict:
        if not isinstance(code, str) or not code.strip() or len(code) > 12000:
            return {"ok": False, "error": "code는 1–12000자 Python이어야 합니다."}
        try:
            response = self.rpc("tools/call", {"name": "aws___run_script", "arguments": {"code": code}})
        finally:
            self.close()
        result = response.get("result") or {}
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if response.get("error") or result.get("isError"):
            return {"ok": False, "error": str(response.get("error") or text)[:1500]}
        data = result.get("structuredContent")
        if not data:
            try:
                data = json.loads(text)
            except ValueError:
                data = {"text": text}
        return {"ok": True, "kind": "evidence",
                "summary": "실제 AWS 조회 · Agent Toolkit for AWS / aws___run_script",
                "data": {"source": "AWS MCP Server", "endpoint_region": self.region, "result": data}}
