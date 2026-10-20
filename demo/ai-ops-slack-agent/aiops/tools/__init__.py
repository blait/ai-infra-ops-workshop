"""In-process SDK MCP server with a few task-level, read-only tools.

BP (Writing effective tools for agents)
- Few task-level tools instead of 1:1 API wrappers; namespaced by service (cloudwatch_*, logs_*,
  cloudtrail_*, cost_*, runbook_*) under one MCP server "aiops" -> mcp__aiops__<tool>.
- Results are summaries with an evidence id, capped at AIOPS_TOOL_OUTPUT_MAX_CHARS.
- Errors are actionable (e.g. unknown metric -> list of valid metrics).
- Least privilege per route: `build_server()` registers only the tools the route needs, so tools
  outside the route do not even appear in the model's tool list.

There is deliberately NO tool that executes a change. `runbook_propose` only stores a PENDING
approval record (aiops/approvals.py) and says so in its result.

Where the work runs (ToolDeps.remote):
- None (local mode): tools/core.py runs in this process against the mock/aws backend.
- a GatewayToolClient (deployed mode): every tool except evidence_get is sent to Amazon Bedrock
  AgentCore Gateway (SigV4, tools/call). The Gateway's Cedar policy engine (ENFORCE) allows or denies
  the call before the Lambda target runs. Evidence ids, size caps and audit stay here, so the model
  sees exactly the same tool contract in both modes. The PreToolUse hook still runs first.

Documentation tools (aws_docs_search / aws_docs_read, aiops/docs.py) are read-only lookups in the AWS
Knowledge MCP Server (local: direct; deployed: AgentCore Gateway MCP-server target + Cedar permit).
They do not create evidence ids. The wrapper records every URL the server returned in
RunContext.doc_hits; the manual-fix gate trusts only those URLs (not URLs typed by the model).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

from ..audit import AuditLog
from ..aws_toolkit import TOOLKIT_SPEC
from ..docs import DOC_TOOLS, READ_URL_PREFIXES, REMOTE_READ, REMOTE_SEARCH, DocsClient, DocsError, \
    doc_tool_specs, norm_url, parse_read, parse_search
from ..notes import NotesStore
from ..runbooks import RunbookRegistry
from .backends import Backend
from .core import ALL_TOOLS, LOCAL_ONLY_TOOLS, READ_TOOLS, RUNBOOK_TOOLS, Caller, ToolCore, metric_hint, tool_specs
from .summarize import fit_json

if TYPE_CHECKING:
    from ..approvals import ApprovalService

__all__ = ["ALL_TOOLS", "DOC_TOOLS", "READ_TOOLS", "RUNBOOK_TOOLS", "SERVER_NAME", "Caller", "RemoteToolCaller", "RunContext",
           "ToolDeps", "build_server", "build_tools", "call_key", "fq"]

SERVER_NAME = "aiops"


def fq(name: str) -> str:
    """Fully-qualified tool name as seen by Claude Code."""
    return f"mcp__{SERVER_NAME}__{name}"


@dataclass
class RunContext:
    """Per-request state shared by tools and hooks (never shown to the model as-is)."""

    thread_id: str
    user_id: str
    route: str
    channel_id: str | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    evidence_created: list[str] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    hook_denials: list[dict] = field(default_factory=list)
    gateway_calls: list[dict] = field(default_factory=list)
    proposal_records: dict[str, dict] = field(default_factory=dict)
    doc_hits: dict[str, dict] = field(default_factory=dict)  # url -> {title, text, tool}: filled by code only
    pending_agent: dict[str, str] = field(default_factory=dict)
    progress: Callable[[str], Awaitable[None]] | None = None

    async def notify(self, message: str) -> None:
        if self.progress:
            try:
                await self.progress(message)
            except Exception:  # noqa: BLE001 - progress is best-effort
                pass


class RemoteToolCaller(Protocol):
    """Deployed mode: aiops/gateway_client.GatewayToolClient."""

    async def call(self, tool: str, args: dict) -> tuple[dict, dict]:
        """Returns (envelope, call_info). call_info is logged (target, latency, policy decision)."""
        ...


@dataclass
class ToolDeps:
    backend: Backend
    notes: NotesStore
    runbooks: RunbookRegistry
    approvals: ApprovalService | None
    audit: AuditLog
    max_chars: int = 3500
    log_row_cap: int = 200
    display_tz: str = "Asia/Seoul"
    remote: RemoteToolCaller | None = None
    docs: DocsClient | None = None
    _core: ToolCore | None = field(default=None, repr=False)

    @property
    def core(self) -> ToolCore:
        if self._core is None:
            self._core = ToolCore(self.backend, self.runbooks, self.approvals, self.display_tz, self.log_row_cap)
        return self._core

    async def call_core(self, ctx: RunContext, name: str, args: dict) -> dict:
        """Run a core tool locally or through the Gateway. Returns the envelope."""
        caller = Caller(thread_id=ctx.thread_id, user_id=ctx.user_id, channel_id=ctx.channel_id,
                        known_evidence_ids=sorted(self.notes.evidence_ids(ctx.thread_id)))
        if self.remote is None:
            return await self.core.call(name, args, caller)
        sent = dict(args)
        sent.pop("caller", None)  # the model never supplies the trusted context
        if name == "runbook_propose":
            sent["caller"] = caller.to_json()
        envelope, info = await self.remote.call(name, sent)
        ctx.gateway_calls.append(info)
        self.audit.write("gateway_call", thread_id=ctx.thread_id, run_id=ctx.run_id, **info)
        return envelope


def call_key(tool_name: str, args: dict) -> str:
    return tool_name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)


def _text(payload: str, is_error: bool = False) -> dict:
    out: dict[str, Any] = {"content": [{"type": "text", "text": payload}]}
    if is_error:
        out["is_error"] = True
    return out


def build_tools(ctx: RunContext, deps: ToolDeps) -> dict[str, Any]:
    from claude_agent_sdk import SdkMcpTool, ToolAnnotations

    services = deps.backend.services()
    specs = {**tool_specs(sorted(services), metric_hint(services)), **TOOLKIT_SPEC}
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    propose_ann = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

    def error(tool_name: str, message: str) -> dict:
        deps.audit.write("tool_error", thread_id=ctx.thread_id, run_id=ctx.run_id, tool=tool_name, error=message)
        return _text(json.dumps({"error": message}, ensure_ascii=False), is_error=True)

    async def finish(tool_name: str, args: dict, summary: str, data: Any) -> dict:
        """Register evidence, cap size, audit, return MCP content."""
        agent = ctx.pending_agent.pop(call_key(fq(tool_name), args), None)

        def render(ev_id: str) -> str:
            return fit_json({"evidence_id": ev_id, "tool": tool_name, "summary": summary,
                             "tz": f"{deps.display_tz} (시각은 현지 표기)", "data": data}, deps.max_chars)

        ev_id, text = deps.notes.add_evidence(ctx.thread_id, tool=tool_name, args=args, summary=summary,
                                              render=render, agent=agent)
        ctx.evidence_created.append(ev_id)
        deps.audit.write("tool_result", thread_id=ctx.thread_id, run_id=ctx.run_id, tool=tool_name, agent=agent,
                         evidence_id=ev_id, chars=len(text), truncated='"_truncated":true' in text, summary=summary,
                         via="gateway" if deps.remote else "local")
        await ctx.notify(f"근거 {ev_id} 수집 ({tool_name}{' · ' + agent if agent else ''}): {summary[:70]}")
        return _text(text)

    async def evidence_get(args):
        ev = deps.notes.get_evidence(ctx.thread_id, args.get("evidence_id", ""))
        if ev is None:
            known = sorted(deps.notes.evidence_ids(ctx.thread_id))
            return error("evidence_get", f"이 스레드에 {args.get('evidence_id')!r} 근거가 없습니다. 있는 ID: {known[-15:]}")
        deps.audit.write("evidence_read", thread_id=ctx.thread_id, run_id=ctx.run_id, evidence_id=ev["id"])
        return _text(ev["payload"][: deps.max_chars])

    def make_handler(name: str):
        async def handler(args):
            args = dict(args or {})
            env = await deps.call_core(ctx, name, args)
            if not env.get("ok"):
                return error(name, env.get("error") or "도구 실행 실패")
            if env.get("kind") == "evidence":
                return await finish(name, args, env["summary"], env["data"])
            body = env.get("body") or {}
            if name == "runbook_list":
                deps.audit.write("runbook_list", thread_id=ctx.thread_id, run_id=ctx.run_id,
                                 count=len(body.get("runbooks", [])))
                return _text(fit_json(body, deps.max_chars))
            if name == "runbook_get":
                deps.audit.write("runbook_get", thread_id=ctx.thread_id, run_id=ctx.run_id, runbook_id=body.get("id"))
                return _text(fit_json(body, deps.max_chars))
            if name == "runbook_propose":
                approval_id = env.get("approval_id") or body.get("approval_id")
                ctx.proposals.append(approval_id)
                if isinstance(env.get("record"), dict):  # written by trusted code (local store / propose Lambda)
                    ctx.proposal_records[approval_id] = env["record"]
                await ctx.notify(f"변경 제안 기록 {approval_id} 생성 (승인 대기)")
                return _text(json.dumps(body, ensure_ascii=False))
            return _text(fit_json(body, deps.max_chars))

        return handler

    def record_doc(url: str, title: str, text: str, tool: str) -> str:
        key = norm_url(url)
        if not key:
            return ""
        canonical = next((u for u in ctx.doc_hits if norm_url(u) == key), url)
        hit = ctx.doc_hits.setdefault(canonical, {"title": title, "text": "", "tool": tool})
        if text and text not in hit["text"]:
            hit["text"] = (hit["text"] + "\n" + text).strip()[:8000]
        hit["title"] = hit["title"] or title
        return canonical

    async def docs_call(tool_name: str, remote: str, args: dict) -> tuple[str | None, dict]:
        if deps.docs is None:
            return None, error(tool_name, "문서 도구가 설정되지 않았습니다(AIOPS_DOCS_MCP_URL / AIOPS_DOCS_GATEWAY_TARGET)")
        try:
            text, info = await deps.docs.call(remote, args)
        except DocsError as e:
            if e.info.get("gateway_tool"):
                ctx.gateway_calls.append(e.info)
                deps.audit.write("gateway_call", thread_id=ctx.thread_id, run_id=ctx.run_id, **e.info)
            return None, error(tool_name, e.message)
        if info.get("gateway_tool"):
            ctx.gateway_calls.append(info)
            deps.audit.write("gateway_call", thread_id=ctx.thread_id, run_id=ctx.run_id, **info)
        return text, info

    async def aws_docs_search(args):
        args = dict(args or {})
        phrase = str(args.get("search_phrase") or "").strip()[:300]
        if not phrase:
            return error("aws_docs_search", "search_phrase가 필요합니다")
        try:
            limit = max(1, min(5, int(args.get("limit") or 3)))
        except (TypeError, ValueError):
            limit = 3
        text, info = await docs_call("aws_docs_search", REMOTE_SEARCH, {"search_phrase": phrase, "limit": limit})
        if text is None:
            return info
        try:
            rows = parse_search(text)[:limit]
        except DocsError as e:
            return error("aws_docs_search", e.message)
        results = []
        for r in rows:
            url = record_doc(r["url"], r["title"], r["context"][:4000], "aws_docs_search")
            results.append({"url": url, "title": r["title"], "context": r["context"][:1500]})
        deps.audit.write("doc_tool_result", thread_id=ctx.thread_id, run_id=ctx.run_id, tool="aws_docs_search",
                         query=phrase, urls=[r["url"] for r in results], via=info.get("via"), ms=info.get("ms"))
        await ctx.notify(f"AWS 문서 검색 '{phrase[:50]}': {len(results)}건")
        return _text(fit_json({"source": "AWS Knowledge MCP (공식 문서, 읽기 전용)", "results": results,
                               "note": "초안의 각 단계 doc_urls에는 여기 나온 url만 쓴다"}, deps.max_chars + 2500))

    async def aws_docs_read(args):
        args = dict(args or {})
        url = str(args.get("url") or "").strip()
        known = {norm_url(u) for u in ctx.doc_hits}
        if norm_url(url) not in known and not url.startswith(READ_URL_PREFIXES):
            return error("aws_docs_read", f"AWS 문서 URL만 읽을 수 있습니다(허용 접두사 {list(READ_URL_PREFIXES)}). "
                                          "aws_docs_search 결과의 url을 쓰세요.")
        try:
            max_len = max(1000, min(6000, int(args.get("max_length") or 4000)))
            start = max(0, int(args.get("start_index") or 0))
        except (TypeError, ValueError):
            max_len, start = 4000, 0
        text, info = await docs_call("aws_docs_read", REMOTE_READ,
                                     {"requests": [{"url": url, "max_length": max_len, "start_index": start}]})
        if text is None:
            return info
        try:
            rows = parse_read(text)
        except DocsError as e:
            return error("aws_docs_read", e.message)
        ok = [r for r in rows if r["content"] and not r.get("error_code")]
        if not ok:
            return error("aws_docs_read", f"문서를 읽지 못했습니다: {[r.get('error_code') or r.get('status') for r in rows]}")
        r = ok[0]
        canonical = record_doc(r["url"] or url, "", r["content"][:6000], "aws_docs_read")
        deps.audit.write("doc_tool_result", thread_id=ctx.thread_id, run_id=ctx.run_id, tool="aws_docs_read",
                         urls=[canonical], chars=len(r["content"]), via=info.get("via"), ms=info.get("ms"))
        await ctx.notify(f"AWS 문서 읽기: {canonical[:90]}")
        return _text(json.dumps({"url": canonical, "content": r["content"][:max_len], "truncated": r["truncated"],
                                 "end_index": r["end_index"], "total_length": r["total_length"]}, ensure_ascii=False))

    out = {}
    for name in (*ALL_TOOLS, *TOOLKIT_SPEC):
        description, schema = specs[name]
        handler = evidence_get if name in LOCAL_ONLY_TOOLS else make_handler(name)
        out[name] = SdkMcpTool(name=name, description=description, input_schema=schema, handler=handler,
                               annotations=propose_ann if name == "runbook_propose" else ro)
    docs_ann = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    for name, (description, schema) in doc_tool_specs().items():
        out[name] = SdkMcpTool(name=name, description=description, input_schema=schema,
                               handler=aws_docs_search if name == "aws_docs_search" else aws_docs_read,
                               annotations=docs_ann)
    return out


def build_server(ctx: RunContext, deps: ToolDeps, names: list[str] | tuple[str, ...]):
    from claude_agent_sdk import create_sdk_mcp_server

    tools = build_tools(ctx, deps)
    unknown = set(names) - set(tools)
    if unknown:
        raise ValueError(f"unknown tools {unknown}")
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=[tools[n] for n in names])
