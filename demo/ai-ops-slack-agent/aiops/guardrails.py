"""Guardrails enforced in code around every Claude Agent SDK run.

Layering (permission boundaries > detection, research/23 §3):
  1. Built-in tools removed: ClaudeAgentOptions.tools = [] (or ["Agent"] for the investigator),
     plus disallowed_tools for Bash/Write/Edit/WebFetch/... as belt and braces.
  2. Only route-scoped MCP tools are registered (aiops/tools build_server).
  3. PreToolUse hook (this file): explicit allowlist per route and per subagent; everything else is
     denied. It fails CLOSED: any exception inside the hook becomes a deny (Claude Code treats a
     crashing hook as a pass, research/02 §3-3).
  4. permission_mode="dontAsk": anything not pre-approved is denied instead of prompting.
  5. setting_sources=[] + strict_mcp_config=True: no user/project settings, CLAUDE.md, hooks or MCP
     servers from the host leak into the agent.
  6. Secrets scrubbed from the subprocess env; empty working directory; verbatim prompts (no
     @file expansion or slash commands from Slack text).
  7. max_turns, max_budget_usd, subagent depth/concurrency caps.
The real boundary for AWS is IAM: the agent role is read-only (iam/agent-readonly-policy.json).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .audit import AuditLog
from .config import SCRUBBED_ENV_VARS, Settings
from .tools import RunContext, call_key, fq

AGENT_TOOL_NAMES = frozenset({"Agent", "Task"})
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"

# Removed from the model's context entirely (bare-name deny rules). The hook denies anything not
# allowlisted anyway; this list documents intent and survives a misconfigured `tools` option.
BUILTIN_DENY = (
    "Bash", "BashOutput", "KillShell", "Monitor", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "TodoWrite", "Skill", "SlashCommand",
    "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "Workflow", "SendMessage", "CronCreate",
)


@dataclass(frozen=True)
class ToolPolicy:
    """Which tools each actor in a run may call. Names are fully qualified (mcp__aiops__x)."""

    route: str
    main_tools: frozenset[str]
    subagent_tools: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def build(cls, route: str, main: list[str], subagents: dict[str, list[str]] | None = None,
              structured_output: bool = False) -> "ToolPolicy":
        main_fq = {fq(n) for n in main}
        if structured_output:
            main_fq.add(STRUCTURED_OUTPUT_TOOL)
        subs = {name: frozenset(fq(n) for n in tools) for name, tools in (subagents or {}).items()}
        return cls(route=route, main_tools=frozenset(main_fq), subagent_tools=subs)


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def decide_tool_use(policy: ToolPolicy, input_data: dict) -> tuple[str, str, dict | None]:
    """Pure decision function: returns (decision, reason, updated_input)."""
    tool = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input") or {}
    agent = input_data.get("agent_type")  # set only inside a subagent

    if tool in AGENT_TOOL_NAMES:
        if agent:
            return "deny", "하위 에이전트는 다른 에이전트를 만들 수 없습니다.", None
        sub = tool_input.get("subagent_type")
        if sub not in policy.subagent_tools:
            return "deny", f"허용되지 않은 하위 에이전트: {sub!r}. 허용: {sorted(policy.subagent_tools)}", None
        # Force foreground so the lead waits for every scout before concluding.
        return "allow", f"subagent {sub}", {**tool_input, "run_in_background": False}

    if agent:
        allowed = policy.subagent_tools.get(agent)
        if allowed is None:
            return "deny", f"알 수 없는 하위 에이전트 {agent!r}", None
    else:
        allowed = policy.main_tools
    if tool not in allowed:
        who = agent or "main"
        return "deny", f"{tool}은(는) 이 경로({policy.route}, {who})에서 허용된 도구가 아닙니다.", None
    return "pass", "allowlisted", None


def make_pre_tool_use_hook(policy: ToolPolicy, ctx: RunContext, audit: AuditLog):
    async def pre_tool_use(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        try:
            decision, reason, updated = decide_tool_use(policy, input_data)
            tool = input_data.get("tool_name", "")
            agent = input_data.get("agent_type")
            audit.write("tool_call", thread_id=ctx.thread_id, run_id=ctx.run_id, route=policy.route, tool=tool,
                        agent=agent, decision=decision, reason=reason,
                        input=json.dumps(input_data.get("tool_input"), ensure_ascii=False)[:400])
            ctx.tool_calls.append({"tool": tool, "agent": agent, "decision": decision})
            if decision == "deny":
                ctx.hook_denials.append({"tool": tool, "agent": agent, "reason": reason})
                return _deny(reason)
            if decision == "allow":
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                               "permissionDecisionReason": reason, "updatedInput": updated}}
            if tool.startswith("mcp__") and agent:
                ctx.pending_agent[call_key(tool, input_data.get("tool_input") or {})] = agent
            return {}
        except Exception as exc:  # fail closed
            audit.write("hook_error", thread_id=ctx.thread_id, run_id=ctx.run_id, error=repr(exc))
            return _deny(f"guardrail hook error: {exc!r}")

    return pre_tool_use


def make_post_tool_use_hook(ctx: RunContext, audit: AuditLog):
    async def post_tool_use(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        try:
            resp = input_data.get("tool_response")
            size = len(json.dumps(resp, ensure_ascii=False, default=str)) if resp is not None else 0
            audit.write("tool_done", thread_id=ctx.thread_id, run_id=ctx.run_id, tool=input_data.get("tool_name"),
                        agent=input_data.get("agent_type"), response_chars=size,
                        duration_ms=input_data.get("duration_ms"))
        except Exception:  # noqa: BLE001 - audit must never break the run
            pass
        return {}

    return post_tool_use


def subprocess_env(settings: Settings) -> dict[str, str]:
    """Env for the Claude Code subprocess: Bedrock selection, caps, and scrubbed secrets."""
    env = {
        "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",  # no general-purpose subagent with all tools
        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(settings.max_concurrent_subagents),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_AGENT_SDK_CLIENT_APP": "aiops-slack-agent/0.1",
    }
    if settings.use_bedrock:
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        if settings.bedrock_region:
            env["AWS_REGION"] = settings.bedrock_region
    for name in SCRUBBED_ENV_VARS:
        env[name] = ""
    return env
