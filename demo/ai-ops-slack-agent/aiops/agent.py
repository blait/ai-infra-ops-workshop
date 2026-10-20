"""Claude Agent SDK runs: investigator (+ parallel scouts), separate evaluator, change proposer.

BP mapping
- Agent loop gather -> act -> verify (Agent SDK blog): scouts gather, the lead analyses, every
  claim must cite an evidence id, and code gates verify the ids before anything is posted.
- Parallel subagents (Building effective agents: parallelization/sectioning; multi-agent research
  system): metrics/logs/changes scouts are SDK AgentDefinitions with disjoint tool sets and clean
  contexts; the PreToolUse hook forces them to run in the foreground so the lead waits for all.
- Separate evaluator (Harness design: "agents tend to respond by confidently praising the work"):
  a second run with NO tools reads the draft against the raw evidence the tools returned and can
  only lower confidence; one revision round max.
- Grounded manual remediation draft (aiops/manual_fix.py): a drafter with doc-search tools only, a
  code gate (every step needs a URL the doc tool returned), and a tool-less reviewer that checks each
  step against the doc snippets + evidence. A human executes the draft; nothing here can.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    ToolUseBlock,
    query,
)

from . import manual_fix as mf
from . import prompts
from .audit import AuditLog
from .config import Settings
from .guardrails import (
    BUILTIN_DENY,
    ToolPolicy,
    make_post_tool_use_hook,
    make_pre_tool_use_hook,
    subprocess_env,
)
from .notes import EVIDENCE_ID_RE
from .tools import DOC_TOOLS, READ_TOOLS, RunContext, ToolDeps, build_server, fq

log = logging.getLogger("aiops.agent")
_RANK = {c: i for i, c in enumerate(prompts.CONFIDENCE)}


@dataclass
class RunSpec:
    name: str
    system_prompt: str
    prompt: str
    tools: list[str] = field(default_factory=list)  # short aiops tool names for the main thread
    agents: dict[str, dict] | None = None  # name -> {description, prompt, tools}
    output_schema: dict | None = None
    model: str | None = None
    max_turns: int = 12
    effort: str | None = None  # low|medium|high|xhigh|max (None = model default)


@dataclass
class RunResult:
    name: str
    text: str | None = None
    structured: Any = None
    cost_usd: float = 0.0
    turns: int = 0
    duration_ms: int = 0
    session_id: str | None = None
    is_error: bool = False
    error: str | None = None
    subtype: str = ""
    tool_uses: list[dict] = field(default_factory=list)


class AgentRunner(Protocol):
    async def run(self, spec: RunSpec, ctx: RunContext) -> RunResult: ...


class SdkAgentRunner:
    """Runs a RunSpec through claude_agent_sdk.query() with all guardrails applied."""

    def __init__(self, settings: Settings, deps: ToolDeps, audit: AuditLog):
        self.settings = settings
        self.deps = deps
        self.audit = audit

    def build_options(self, spec: RunSpec, ctx: RunContext) -> ClaudeAgentOptions:
        sub_tools = {n: list(a["tools"]) for n, a in (spec.agents or {}).items()}
        all_tools = list(dict.fromkeys(spec.tools + [t for ts in sub_tools.values() for t in ts]))
        policy = ToolPolicy.build(ctx.route, spec.tools, sub_tools, structured_output=spec.output_schema is not None)
        agents = None
        if spec.agents:
            agents = {
                name: AgentDefinition(description=a["description"], prompt=a["prompt"],
                                      tools=[fq(t) for t in a["tools"]], model=self.settings.scout_model,
                                      maxTurns=8, effort=a.get("effort"))
                for name, a in spec.agents.items()
            }
        allowed = [fq(t) for t in all_tools] + (["Agent"] if agents else [])
        options = ClaudeAgentOptions(
            system_prompt=spec.system_prompt,
            tools=["Agent"] if agents else [],
            allowed_tools=allowed,
            disallowed_tools=list(BUILTIN_DENY),
            mcp_servers={"aiops": build_server(ctx, self.deps, all_tools)} if all_tools else {},
            strict_mcp_config=True,
            agents=agents,
            hooks={
                "PreToolUse": [HookMatcher(matcher=None, hooks=[make_pre_tool_use_hook(policy, ctx, self.audit)])],
                "PostToolUse": [HookMatcher(matcher=None, hooks=[make_post_tool_use_hook(ctx, self.audit)])],
            },
            permission_mode="dontAsk",
            setting_sources=[],
            max_turns=spec.max_turns,
            max_budget_usd=self.settings.max_budget_usd,
            model=spec.model or self.settings.model,
            output_format={"type": "json_schema", "schema": spec.output_schema} if spec.output_schema else None,
            cwd=str(self.settings.agent_cwd),
            env=subprocess_env(self.settings),
            verbatim_prompts=True,
            effort=spec.effort,
            stderr=lambda line: log.debug("claude stderr: %s", line.rstrip()),
        )
        return options

    async def run(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        self.settings.agent_cwd.mkdir(parents=True, exist_ok=True)
        options = self.build_options(spec, ctx)
        result = RunResult(name=spec.name)
        started = time.monotonic()
        self.audit.write("run_started", thread_id=ctx.thread_id, run_id=ctx.run_id, run=spec.name, route=ctx.route,
                         model=options.model, tools=options.allowed_tools, subagents=sorted(spec.agents or {}))
        try:
            async for msg in query(prompt=spec.prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            result.tool_uses.append({"name": block.name, "parent": msg.parent_tool_use_id,
                                                     "input": block.input})
                elif isinstance(msg, ResultMessage):
                    result.text = msg.result
                    result.structured = msg.structured_output
                    result.cost_usd = msg.total_cost_usd or 0.0
                    result.turns = msg.num_turns
                    result.duration_ms = msg.duration_ms
                    result.session_id = msg.session_id
                    result.is_error = msg.is_error
                    result.subtype = msg.subtype
                    if msg.is_error:
                        result.error = "; ".join(msg.errors or []) or msg.subtype
        except Exception as exc:  # noqa: BLE001 - surface any SDK/CLI failure as a result
            result.is_error = True
            result.error = f"{type(exc).__name__}: {exc}"
        if result.structured is None and result.text and spec.output_schema:
            result.structured = _parse_json_block(result.text)
        result.duration_ms = result.duration_ms or int((time.monotonic() - started) * 1000)
        self.audit.write("run_finished", thread_id=ctx.thread_id, run_id=ctx.run_id, run=spec.name,
                         is_error=result.is_error, error=result.error, subtype=result.subtype, turns=result.turns,
                         cost_usd=round(result.cost_usd, 4), duration_ms=result.duration_ms,
                         session_id=result.session_id,
                         tool_uses=[u["name"] for u in result.tool_uses])
        return result


def _parse_json_block(text: str) -> Any:
    text = text.strip()
    if "```" in text:
        text = text.split("```")[1].removeprefix("json").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, IndexError):
        return None


# ============================================================================== flows
@dataclass
class ManualFix:
    """Result of Flows.draft_manual_fix. `fix` is what may be shown (after gate + review)."""

    fix: dict
    issues: list[str]
    runs: list[RunResult]
    error: str | None = None

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.runs)

    @property
    def has_steps(self) -> bool:
        return bool(self.fix.get("steps"))


@dataclass
class Investigation:
    rca: dict
    draft: dict | None
    critique: dict | None
    gate_issues: list[str]
    runs: list[RunResult]
    error: str | None = None
    manual_fix: ManualFix | None = None

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.runs) + (self.manual_fix.cost_usd if self.manual_fix else 0.0)


def _min_confidence(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) <= _RANK.get(b, 0) else b


def gate_rca(rca: dict, valid_ids: set[str]) -> tuple[dict, list[str]]:
    """Programmatic gate: drop citations to evidence that does not exist; downgrade if nothing is left."""
    issues: list[str] = []
    rca = json.loads(json.dumps(rca))  # deep copy
    kept = []
    for e in rca.get("evidence", []):
        ev = (e.get("id") or "").strip()
        if ev in valid_ids:
            kept.append(e)
        else:
            issues.append(f"존재하지 않는 근거 ID 인용 제거: {ev or '(빈 ID)'}")
    rca["evidence"] = kept
    for r in rca.get("ruled_out", []):
        bad = [x for x in r.get("evidence_ids", []) if x not in valid_ids]
        if bad:
            issues.append(f"배제 항목의 근거 ID 제거: {bad}")
            r["evidence_ids"] = [x for x in r.get("evidence_ids", []) if x in valid_ids]
    for t in rca.get("timeline", []):
        if t.get("evidence_id") and t["evidence_id"] not in valid_ids:
            issues.append(f"타임라인 근거 ID 제거: {t['evidence_id']}")
            t["evidence_id"] = None
    if rca.get("confidence") not in _RANK:
        rca["confidence"] = "low"
    if not kept:
        if rca.get("confidence") != "low":
            issues.append("유효한 근거가 없어 확신도를 low로 낮춤")
        rca["confidence"] = "low"
    # every EV-.. mentioned anywhere in free text must exist too
    for ev in set(EVIDENCE_ID_RE.findall(json.dumps(rca, ensure_ascii=False))) - valid_ids:
        issues.append(f"본문에 없는 근거 ID 언급: {ev}")
    return rca, issues


class Flows:
    def __init__(self, settings: Settings, deps: ToolDeps, runner: AgentRunner):
        self.settings = settings
        self.deps = deps
        self.runner = runner
        self.tz = ZoneInfo(settings.display_tz)

    def _now_local(self) -> str:
        return self.deps.backend.now().astimezone(self.tz).strftime("%Y-%m-%d %H:%M %Z")

    def _evidence_dump(self, ctx: RunContext, ids: list[str], max_chars: int = 14000) -> str:
        parts, total = [], 0
        for ev_id in ids:
            ev = self.deps.notes.get_evidence(ctx.thread_id, ev_id)
            if not ev:
                continue
            chunk = f"## {ev_id} ({ev['tool']} {json.dumps(ev['args'], ensure_ascii=False)})\n{ev['payload'][:2500]}\n"
            if total + len(chunk) > max_chars:
                parts.append(f"(근거 {len(ids) - len(parts)}건 생략: 길이 제한)")
                break
            parts.append(chunk)
            total += len(chunk)
        return "\n".join(parts) or "(근거 없음)"

    async def investigate(self, ctx: RunContext, request: str) -> Investigation:
        notes_digest = self.deps.notes.render_for_prompt(ctx.thread_id)
        services = sorted(self.deps.backend.services())
        runs: list[RunResult] = []

        await ctx.notify("조사 시작: 하위 에이전트 3개(지표·로그·변경 이력)가 병렬로 근거를 모읍니다")
        draft_run = await self.runner.run(RunSpec(
            name="investigator", system_prompt=prompts.INVESTIGATOR_SYSTEM,
            prompt=prompts.investigation_prompt(request, notes_digest, services, self._now_local()),
            tools=list(READ_TOOLS), agents=prompts.SCOUTS, output_schema=prompts.RCA_SCHEMA,
            max_turns=self.settings.max_turns), ctx)
        runs.append(draft_run)
        if draft_run.is_error or not isinstance(draft_run.structured, dict):
            return Investigation(rca={}, draft=None, critique=None, gate_issues=[], runs=runs,
                                 error=draft_run.error or "조사 에이전트가 구조화된 결과를 내지 못했습니다")

        valid = self.deps.notes.evidence_ids(ctx.thread_id)
        draft, issues = gate_rca(draft_run.structured, valid)
        # Evidence the draft cites goes first (in citation order) so the length cap of the dump can only
        # drop uncited evidence, never the facts the reviewer must check.
        cited_first = list(dict.fromkeys(
            [e.get("id") for e in draft.get("evidence", [])]
            + [x for r in draft.get("ruled_out", []) for x in r.get("evidence_ids", [])]
            + [t.get("evidence_id") for t in draft.get("timeline", []) if t.get("evidence_id")]))
        cited_first = [x for x in cited_first if x]
        cited = cited_first + sorted(set(ctx.evidence_created) - set(cited_first))
        evidence_dump = self._evidence_dump(ctx, cited)
        draft_json = json.dumps(draft, ensure_ascii=False, indent=1)

        await ctx.notify(f"초안 완료(근거 {len(cited)}건). 별도 검토자가 근거 원문과 대조 중…")
        crit_run = await self.runner.run(RunSpec(
            name="evaluator", system_prompt=prompts.EVALUATOR_SYSTEM,
            prompt=prompts.evaluator_prompt(request, draft_json, evidence_dump),
            output_schema=prompts.CRITIQUE_SCHEMA, model=self.settings.effective_evaluator_model, max_turns=3,
            effort="medium"), ctx)
        runs.append(crit_run)
        critique = crit_run.structured if isinstance(crit_run.structured, dict) else None
        final = draft
        if critique is None:
            issues.append("검토자 실행 실패: 확신도를 low로 제한")
            final["confidence"] = "low"
        elif critique.get("verdict") == "revise":
            await ctx.notify(f"검토 의견 {len(critique.get('issues', []))}건 반영해 수정 중…")
            rev_run = await self.runner.run(RunSpec(
                name="reviser", system_prompt=prompts.REVISER_SYSTEM,
                prompt=prompts.reviser_prompt(request, draft_json, json.dumps(critique, ensure_ascii=False, indent=1),
                                              evidence_dump),
                output_schema=prompts.RCA_SCHEMA, max_turns=3, effort="low"), ctx)
            runs.append(rev_run)
            if isinstance(rev_run.structured, dict):
                final, rev_issues = gate_rca(rev_run.structured, valid)
                issues += rev_issues
            else:
                issues.append("수정 실행 실패: 초안을 쓰되 검토자 확신도 상한을 적용")
        if critique is not None:
            capped = _min_confidence(final.get("confidence", "low"), critique.get("max_supported_confidence", "low"))
            if capped != final.get("confidence"):
                issues.append(f"확신도 {final.get('confidence')} → {capped} (검토자 상한)")
            final["confidence"] = capped
        self.deps.notes.apply_rca(ctx.thread_id, final, reviewer_note=(critique or {}).get("reviewer_note"))
        fix = None
        if self.settings.manual_fix_draft:
            situation = "조사 결과(검토 반영 최종본):\n" + json.dumps(
                {k: final.get(k) for k in ("service", "summary", "hypothesis", "confidence", "evidence", "next_checks")},
                ensure_ascii=False, indent=1)
            fix = await self.draft_manual_fix(ctx, request, situation, cited_ids=cited)
        return Investigation(rca=final, draft=draft, critique=critique, gate_issues=issues, runs=runs, manual_fix=fix)

    # ------------------------------------------------------------------ manual remediation draft
    def _runbook_digest(self) -> str:
        rows = [f"- {rb.id}: {rb.title} — {rb.summary}" for rb in self.deps.runbooks.list() if rb.executable]
        return "\n".join(rows) or "(없음)"

    def _evidence_text(self, ctx: RunContext) -> str:
        parts = []
        for ev_id in sorted(self.deps.notes.evidence_ids(ctx.thread_id)):
            ev = self.deps.notes.get_evidence(ctx.thread_id, ev_id) or {}
            parts.append(str(ev.get("payload") or ""))
        return "\n".join(parts)

    async def draft_manual_fix(self, ctx: RunContext, request: str, situation: str,
                               cited_ids: list[str] | None = None) -> ManualFix:
        """drafter (doc tools only) -> code gate -> tool-less reviewer -> apply review (code)."""
        runs: list[RunResult] = []
        await ctx.notify("수동 조치 초안 작성 중: AWS 공식 문서에서 근거를 찾습니다(실행은 사람이 합니다)")
        draft_run = await self.runner.run(RunSpec(
            name="fix-drafter", system_prompt=prompts.FIX_DRAFTER_SYSTEM,
            prompt=prompts.fix_drafter_prompt(request, situation, self._runbook_digest(),
                                              self.deps.notes.render_for_prompt(ctx.thread_id), self._now_local()),
            tools=[*DOC_TOOLS, "evidence_get"], output_schema=mf.MANUAL_FIX_SCHEMA, max_turns=10,
            effort="low"), ctx)
        runs.append(draft_run)
        if draft_run.is_error or not isinstance(draft_run.structured, dict):
            return ManualFix(fix={}, issues=[], runs=runs, error=draft_run.error or "초안 작성자가 결과를 내지 못했습니다")
        evidence_text = self._evidence_text(ctx)
        gated, issues = mf.gate_manual_fix(draft_run.structured, ctx.doc_hits, evidence_text)
        self.deps.audit.write("manual_fix_gate", thread_id=ctx.thread_id, run_id=ctx.run_id,
                              doc_urls_returned=sorted(ctx.doc_hits), kept=gated["doc_urls"],
                              items={s: len(gated[s]) for s, _ in mf.SECTIONS}, issues=issues)
        if not any(gated[s] for s, _ in mf.SECTIONS):
            gated["reviewer_note"] = "근거 있는 단계가 없어 검토할 것이 없음"
            return ManualFix(fix=gated, issues=issues, runs=runs)
        await ctx.notify(f"초안 게이트 통과 {sum(len(gated[s]) for s, _ in mf.SECTIONS)}개 단계. "
                         "별도 검토자가 문서 발췌·근거와 대조 중…")
        ids = list(dict.fromkeys((cited_ids or []) + sorted(set(ctx.evidence_created))))
        evidence_dump = self._evidence_dump(ctx, ids or sorted(self.deps.notes.evidence_ids(ctx.thread_id)),
                                            max_chars=8000)
        review_run = await self.runner.run(RunSpec(
            name="fix-reviewer", system_prompt=prompts.FIX_REVIEWER_SYSTEM,
            prompt=prompts.fix_reviewer_prompt(request, mf.review_payload(gated),
                                               mf.doc_snippets(gated, ctx.doc_hits), evidence_dump),
            output_schema=mf.FIX_REVIEW_SCHEMA, model=self.settings.effective_evaluator_model, max_turns=3,
            effort="medium"), ctx)
        runs.append(review_run)
        final, review_issues = mf.apply_review(gated, review_run.structured if isinstance(review_run.structured, dict)
                                               else None)
        issues += review_issues
        self.deps.audit.write("manual_fix_review", thread_id=ctx.thread_id, run_id=ctx.run_id,
                              kept={s: [i["ref"] for i in final[s]] for s, _ in mf.SECTIONS},
                              destructive=[i["ref"] for s, _ in mf.SECTIONS for i in final[s] if i.get("destructive")],
                              issues=review_issues)
        return ManualFix(fix=final, issues=issues, runs=runs)

    async def propose_change(self, ctx: RunContext, request: str) -> tuple[dict, RunResult]:
        notes_digest = self.deps.notes.render_for_prompt(ctx.thread_id)
        await ctx.notify("변경 요청 확인 중: 허용 목록 runbook과 조사 노트의 근거를 대조합니다")
        run = await self.runner.run(RunSpec(
            name="change", system_prompt=prompts.CHANGE_SYSTEM,
            prompt=prompts.change_prompt(request, notes_digest, self._now_local()),
            tools=["runbook_list", "runbook_get", "runbook_propose", "evidence_get", "logs_error_summary",
                   "cloudwatch_alarm_status"],
            output_schema=prompts.CHANGE_SCHEMA, max_turns=10), ctx)
        out = run.structured if isinstance(run.structured, dict) else {
            "decision": "need_info", "message": run.error or "변경 에이전트가 결과를 내지 못했습니다."}
        # Gate: an approval id counts only if the propose tool created it in THIS run.
        claimed = out.get("approval_id")
        if out.get("decision") == "proposed" and claimed not in ctx.proposals:
            self.deps.audit.write("gate_violation", thread_id=ctx.thread_id, run_id=ctx.run_id,
                                  claimed_approval_id=claimed, created=ctx.proposals)
            out = {**out, "decision": "declined" if not ctx.proposals else "proposed",
                   "approval_id": ctx.proposals[-1] if ctx.proposals else None,
                   "message": out.get("message", "") + "\n(주의: 모델이 말한 승인 ID가 실제 기록과 달라 기록 기준으로 표시)"}
        if out.get("decision") != "proposed" and ctx.proposals:
            out = {**out, "decision": "proposed", "approval_id": ctx.proposals[-1]}
        return out, run

