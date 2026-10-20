"""Fixed report workflows: prompt chaining with programmatic gates.

BP (Building effective agents, "Prompt chaining ... with programmatic checks (gates)"):
  step 1  fetch data with code (no model; same tool core, local or via Gateway) -> evidence EV-NN
  gate 1  completeness: >=2 complete days / data present, else stop without calling a model
  step 2  deterministic table (numbers come from code, never from the model)
  step 3  optional LLM summary (one call, no tools) with the thread-notes digest for context
  gate 2  every number in the summary must exist in the table or notes; otherwise the summary is
          discarded and a template sentence is used instead. The gate result is shown to the user.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from . import prompts
from .agent import AgentRunner, RunSpec
from .tools import RunContext, ToolDeps
from .tools.summarize import fit_json

_NUM = re.compile(r"(?<![\w.])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")


def money(x: float, signed: bool = False) -> str:
    """$1,234.50 / +$155.30 / -$0.19"""
    sign = ("+" if x >= 0 else "-") if signed else ("-" if x < 0 else "")
    return f"{sign}${abs(x):,.2f}"


@dataclass
class WorkflowResult:
    kind: str
    title: str
    table: str
    summary: str
    summary_source: str  # llm | template
    gate_notes: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    ok: bool = True


def extract_numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.findall(text):
        cleaned = m.replace("$", "").replace(",", "").replace("%", "").replace("+", "")
        try:
            out.append(float(cleaned))
        except ValueError:
            continue
    return out


def numbers_grounded(summary: str, allowed_texts: list[str]) -> tuple[bool, list[float]]:
    """Every number in `summary` must appear (allowing rounding) in one of `allowed_texts`."""
    allowed: list[float] = []
    for t in allowed_texts:
        allowed += extract_numbers(t)
        allowed += [float(x) for x in re.findall(r"\d+", t)]  # date/time parts like 10-14, 13:42
    bad = []
    for x in extract_numbers(summary):
        if abs(x) <= 10 and float(x).is_integer():
            continue  # small counts/ordinals ("3건", "1위") are not a hallucination risk worth failing on
        ok = any(abs(x - a) < 0.051 or round(a) == x or round(a, 1) == x or round(abs(a), 1) == x for a in allowed)
        if not ok:
            bad.append(x)
    return (not bad), bad


class Workflows:
    def __init__(self, deps: ToolDeps, runner: AgentRunner | None, display_tz: str, use_llm: bool):
        self.deps = deps
        self.runner = runner
        self.tz = ZoneInfo(display_tz)
        self.use_llm = use_llm and runner is not None

    def _evidence(self, ctx: RunContext, tool: str, args: dict, summary: str, data: dict) -> str:
        def render(ev_id: str) -> str:
            return fit_json({"evidence_id": ev_id, "tool": tool, "summary": summary, "data": data}, self.deps.max_chars)

        ev_id, _ = self.deps.notes.add_evidence(ctx.thread_id, tool=tool, args=args, summary=summary, render=render,
                                                agent="workflow")
        ctx.evidence_created.append(ev_id)
        return ev_id

    async def _summarize(self, ctx: RunContext, table: str, template: str, extra_context: str) -> tuple[str, str, list[str], float]:
        if not self.use_llm:
            return template, "template", ["LLM 요약 끔(AIOPS_WORKFLOW_LLM=0): 템플릿 문장 사용"], 0.0
        notes_digest = self.deps.notes.render_for_prompt(ctx.thread_id, max_chars=1500)
        run = await self.runner.run(RunSpec(
            name="workflow_summary", system_prompt=prompts.WORKFLOW_SUMMARY_SYSTEM,
            prompt=f"# 표\n{table}\n\n# 참고\n{extra_context}\n\n# 스레드 노트\n{notes_digest}\n\n표를 3문장 이내로 요약하라.",
            max_turns=2), ctx)
        text = (run.text or "").strip()
        if run.is_error or not text:
            return template, "template", [f"LLM 요약 실패({run.error}): 템플릿 문장 사용"], run.cost_usd
        ok, bad = numbers_grounded(text, [table, extra_context, notes_digest])
        if not ok:
            self.deps.audit.write("workflow_gate_failed", thread_id=ctx.thread_id, run_id=ctx.run_id,
                                  ungrounded_numbers=bad, llm_summary=text)
            return template, "template", [f"게이트: 표에 없는 숫자 {bad} → LLM 요약 폐기, 템플릿 사용"], run.cost_usd
        if len(text) > 600:
            text = text[:600] + "…"
        return text, "llm", ["게이트 통과: 요약의 숫자가 모두 표에 있음"], run.cost_usd

    # ------------------------------------------------------------------ cost report
    async def cost_report(self, ctx: RunContext, request: str) -> WorkflowResult:
        # step 1: data by code through the same tool core (local or AgentCore Gateway), no model
        env = await self.deps.call_core(ctx, "cost_daily_delta", {"days": 8})
        if not env.get("ok"):  # gate 1
            return WorkflowResult("cost_report", "비용 리포트", "", f"리포트를 만들 수 없습니다: {env.get('error')}",
                                  "template", ["게이트 1 실패: 데이터 부족 → 모델 호출 없이 종료"], ok=False)
        data = env["data"]
        c = data["compare"]
        top = data["top_movers"][0]
        pct = f"{top['delta_pct']:+.1f}%" if top["delta_pct"] is not None else "n/a"
        summary_line = (f"{c['yesterday']} 총 {money(data['total']['yesterday'])} (전일 대비 {money(data['total']['delta'], True)}); "
                        f"최대 변동 {top['service']} {money(top['delta'], True)} ({pct})")
        ev_id = self._evidence(ctx, "cost_daily_delta", {"days": 8}, summary_line, data)

        name_w = 40
        lines = [f"{'서비스':<{name_w - 3}} {c['day_before'][5:]:>10} {c['yesterday'][5:]:>10} {'증감':>10} {'증감%':>8} {'7일평균':>9}"]
        for x in data["top_movers"]:
            p = f"{x['delta_pct']:+.1f}%" if x["delta_pct"] is not None else "n/a"
            lines.append(f"{x['service'][:name_w]:<{name_w}} {x['day_before']:>10,.2f} {x['yesterday']:>10,.2f} "
                         f"{x['delta']:>+10,.2f} {p:>8} {x['avg_7d']:>9,.2f}")
        t = data["total"]
        lines.append(f"{'합계(전체 서비스)':<{name_w - 5}} {t['day_before']:>10,.2f} {t['yesterday']:>10,.2f} {t['delta']:>+10,.2f}")
        table = "\n".join(lines)
        partial = data["partial_today"]
        extra = (f"단위 USD. 비교: {c['yesterday']} vs {c['day_before']}. 7일 평균 구간 {c['avg_window']}. "
                 f"{partial['date']}은 추정치라 제외.")
        template = (f"어제({c['yesterday'][5:]}) 비용은 {money(t['yesterday'])}로 그제보다 {money(t['delta'], True)} 변했습니다. "
                    f"가장 크게 변한 서비스는 {top['service']}({money(top['delta'], True)}, {pct})입니다. "
                    f"오늘({partial['date'][5:] if partial['date'] else '-'}) 값은 추정치라 비교에서 뺐습니다.")
        summary, source, notes, cost = await self._summarize(ctx, table, template, extra)
        self.deps.notes.add_report(ctx.thread_id, "cost_report", summary_line, [ev_id])
        return WorkflowResult("cost_report", f"비용 리포트: {c['yesterday']} vs {c['day_before']} (USD)", table, summary,
                              source, ["게이트 1 통과: 완결된 일별 데이터 2일 이상"] + notes, [ev_id], cost)

    # ------------------------------------------------------------------ alarm report
    async def alarm_report(self, ctx: RunContext, request: str) -> WorkflowResult:
        env = await self.deps.call_core(ctx, "cloudwatch_alarm_status", {"state": "ANY"})
        if not env.get("ok"):
            return WorkflowResult("alarm_report", "알람 현황", "", f"알람을 조회하지 못했습니다: {env.get('error')}",
                                  "template", ["게이트 1 실패: 조회 오류 → 모델 호출 없이 종료"], ok=False)
        alarms = env["data"]["alarms"]
        firing = []
        for a in alarms:
            if a["state"] != "ALARM":
                continue
            since = datetime.fromisoformat(a["since_iso"].replace("Z", "+00:00"))
            firing.append({**a, "since_local": since.astimezone(self.tz).strftime("%m-%d %H:%M"),
                           "minutes": a["for_minutes"]})
        firing.sort(key=lambda a: a["minutes"], reverse=True)
        ok_count = sum(1 for a in alarms if a["state"] == "OK")
        other = len(alarms) - ok_count - len(firing)
        summary_line = f"ALARM {len(firing)}건 / OK {ok_count}건 / 기타 {other}건"
        ev_id = self._evidence(ctx, "cloudwatch_alarm_status", {"state": "ANY"}, summary_line,
                               {"firing": firing, "ok": ok_count, "other": other})
        if not alarms:  # gate 1
            return WorkflowResult("alarm_report", "알람 현황", "", "조회된 알람이 없습니다.", "template",
                                  ["게이트 1: 알람 0건 → 모델 호출 없이 종료"], [ev_id], ok=False)
        lines = [f"{'알람':<34} {'서비스':<17} {'시작':>12} {'지속(분)':>8}"]
        for a in firing:
            lines.append(f"{a['name'][:34]:<34} {a['service'][:18]:<18} {a['since_local']:>12} {a['minutes']:>8}")
        lines.append(f"(OK {ok_count}건, 기타 {other}건)")
        table = "\n".join(lines)
        by_service: dict[str, int] = {}
        for a in firing:
            by_service[a["service"]] = by_service.get(a["service"], 0) + 1
        template = (f"현재 ALARM 상태 알람은 {len(firing)}건입니다"
                    + (": " + ", ".join(f"{s} {n}건" for s, n in by_service.items()) if firing else "")
                    + (f". 가장 오래된 것은 {firing[0]['name']}({firing[0]['since_local']}부터)입니다." if firing else "."))
        summary, source, notes, cost = await self._summarize(ctx, table, template, json.dumps(by_service, ensure_ascii=False))
        self.deps.notes.add_report(ctx.thread_id, "alarm_report", summary_line, [ev_id])
        return WorkflowResult("alarm_report", "알람 현황", table, summary, source,
                              ["게이트 1 통과: 알람 데이터 있음"] + notes, [ev_id], cost)
