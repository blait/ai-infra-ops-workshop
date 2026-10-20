"""Read-only AWS documentation lookup for grounded manual remediation drafts.

Source: the AWS Knowledge MCP Server (remote, no auth): https://knowledge-mcp.global.api.aws
Verified live on 2026-10-19: serverInfo AWSKnowledgeMCP 1.0.0, protocol 2025-03-26, tools
`aws___search_documentation`, `aws___read_documentation`, `aws___list_regions`,
`aws___get_regional_availability`, `aws___retrieve_skill` (all annotated readOnlyHint=true).
This agent uses only search + read.

Where the call goes
- local mode: KnowledgeMcpClient posts JSON-RPC `tools/call` straight to the server (stateless works).
- deployed mode: GatewayDocsClient sends it through AgentCore Gateway, where the server is attached as
  an MCP-server target (no outbound auth) and a Cedar permit allows only the two read tools. The
  gateway tool name is `<target>___aws___search_documentation`.

The model sees two task-level tools (`aws_docs_search`, `aws_docs_read`). The wrapper in
aiops/tools/__init__.py records every URL the server actually returned in RunContext.doc_hits; the
manual-fix gate (aiops/manual_fix.py) accepts only those URLs as grounding.

This module must not import claude_agent_sdk.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
import urllib.error
import urllib.request
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

DOCS_MCP_URL = "https://knowledge-mcp.global.api.aws"
REMOTE_SEARCH = "aws___search_documentation"
REMOTE_READ = "aws___read_documentation"
REMOTE_TOOLS = (REMOTE_SEARCH, REMOTE_READ)
DOC_TOOLS = ("aws_docs_search", "aws_docs_read")
READ_URL_PREFIXES = ("https://docs.aws.amazon.com/", "https://aws.amazon.com/", "https://repost.aws/knowledge-center")


class DocsError(Exception):
    def __init__(self, message: str, outcome: str = "error", info: dict | None = None):
        super().__init__(message)
        self.message = message
        self.outcome = outcome  # error | denied
        self.info = info or {}


class DocsClient(Protocol):
    async def call(self, remote_tool: str, args: dict) -> tuple[str, dict]:
        """Returns (text content of the MCP result, call info for audit). Raises DocsError."""
        ...


def norm_url(url: str | None) -> str:
    """Canonical form for matching: no fragment/query, no trailing slash, lower-case host."""
    if not url:
        return ""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", "", ""))


def doc_tool_specs() -> dict[str, tuple[str, dict]]:
    return {
        "aws_docs_search": (
            "AWS 공식 문서 검색(읽기 전용, AWS Knowledge MCP). 결과마다 url, title, 원문 발췌(context)를 준다. "
            "수동 조치 초안의 모든 명령·콘솔 경로는 이 도구가 돌려준 url로 근거를 대야 한다. "
            "검색어는 영어 키워드가 잘 맞는다(예: 'RDS modify-db-cluster-parameter-group max_connections').",
            {"type": "object", "properties": {
                "search_phrase": {"type": "string", "description": "검색 키워드(서비스명 + CLI 명령/설정 이름)"},
                "limit": {"type": "integer", "description": "결과 수 1~5, 기본 3"}},
             "required": ["search_phrase"]}),
        "aws_docs_read": (
            "aws_docs_search가 돌려준 AWS 문서 페이지의 본문 일부를 마크다운으로 읽는다(읽기 전용). "
            "검색 발췌로 명령 형식·파라미터가 확인되지 않을 때만 쓴다.",
            {"type": "object", "properties": {
                "url": {"type": "string", "description": "aws_docs_search 결과의 url"},
                "start_index": {"type": "integer", "description": "시작 문자 위치(기본 0, 목차의 char 범위 사용)"},
                "max_length": {"type": "integer", "description": "읽을 길이 1000~6000, 기본 4000"}},
             "required": ["url"]}),
    }


# ------------------------------------------------------------------------------ parsing
def _payload(text: str) -> Any:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("content"), dict):
        data = data["content"]
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    return data


def parse_search(text: str) -> list[dict]:
    rows = _payload(text)
    if not isinstance(rows, list):
        raise DocsError(f"문서 검색 응답 형식을 해석하지 못했습니다: {str(text)[:200]}")
    out = []
    for r in rows:
        if isinstance(r, dict) and r.get("url"):
            out.append({"url": str(r["url"]), "title": str(r.get("title") or ""),
                        "context": str(r.get("context") or "")})
    return out


def parse_read(text: str) -> list[dict]:
    rows = _payload(text)
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        raise DocsError(f"문서 읽기 응답 형식을 해석하지 못했습니다: {str(text)[:200]}")
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append({"url": str(r.get("redirected_url") or r.get("url") or ""), "status": r.get("status"),
                        "content": str(r.get("content") or ""), "truncated": bool(r.get("truncated")),
                        "end_index": r.get("end_index"), "total_length": r.get("total_length"),
                        "error_code": r.get("error_code")})
    return out


def _result_text(res: dict) -> tuple[str, bool]:
    result = res.get("result") or {}
    texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
    return "".join(texts), bool(result.get("isError"))


# ------------------------------------------------------------------------------ clients
class KnowledgeMcpClient:
    """Local mode: JSON-RPC straight to the public AWS Knowledge MCP Server (no auth)."""

    def __init__(self, url: str = DOCS_MCP_URL, timeout: float = 30.0, opener=None):
        self.url = url
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self._ids = itertools.count(1)

    def call_sync(self, remote_tool: str, args: dict) -> tuple[str, dict]:
        if remote_tool not in REMOTE_TOOLS:
            raise DocsError(f"허용되지 않은 문서 도구 {remote_tool!r}", "denied")
        body = json.dumps({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                           "params": {"name": remote_tool, "arguments": args}}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        started = time.monotonic()
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                from .gateway_client import _parse_body

                res = _parse_body(resp.read(), resp.headers.get("Content-Type", ""))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise DocsError(f"AWS 문서 서버 호출 실패: {e}") from e
        info = {"tool": remote_tool, "via": "direct", "ms": int((time.monotonic() - started) * 1000)}
        if res.get("error"):
            raise DocsError(f"AWS 문서 서버 오류: {res['error']}", info={**info, "outcome": "error"})
        text, is_error = _result_text(res)
        if is_error:
            raise DocsError(f"AWS 문서 서버 도구 오류: {text[:300]}", info={**info, "outcome": "error"})
        info["outcome"] = "ok"
        return text, info

    async def call(self, remote_tool: str, args: dict) -> tuple[str, dict]:
        return await asyncio.to_thread(self.call_sync, remote_tool, args)


class GatewayDocsClient:
    """Deployed mode: the same server behind AgentCore Gateway (MCP-server target) + Cedar policy."""

    def __init__(self, gateway, target: str):
        self.gateway = gateway  # aiops.gateway_client.GatewayToolClient
        self.target = target

    def call_sync(self, remote_tool: str, args: dict) -> tuple[str, dict]:
        from .gateway_client import TOOL_DELIMITER, classify_error

        if remote_tool not in REMOTE_TOOLS:
            raise DocsError(f"허용되지 않은 문서 도구 {remote_tool!r}", "denied")
        name = f"{self.target}{TOOL_DELIMITER}{remote_tool}"
        started = time.monotonic()
        res = self.gateway.rpc("tools/call", {"name": name, "arguments": args})
        info: dict[str, Any] = {"tool": remote_tool, "gateway_tool": name, "via": "gateway",
                                "ms": int((time.monotonic() - started) * 1000)}
        if res.get("error"):
            err = res["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            outcome = classify_error(msg)
            info.update(outcome=outcome, error=str(msg)[:300], http_status=res.get("_http_status"))
            raise DocsError(f"[{'policy_denied' if outcome == 'denied' else 'gateway_error'}] {msg}", outcome, info)
        text, is_error = _result_text(res)
        if is_error:
            outcome = classify_error(text)
            info.update(outcome=outcome, error=text[:300])
            raise DocsError(f"[{'policy_denied' if outcome == 'denied' else 'gateway_error'}] {text[:300]}",
                            outcome, info)
        info["outcome"] = "ok"
        return text, info

    async def call(self, remote_tool: str, args: dict) -> tuple[str, dict]:
        return await asyncio.to_thread(self.call_sync, remote_tool, args)
