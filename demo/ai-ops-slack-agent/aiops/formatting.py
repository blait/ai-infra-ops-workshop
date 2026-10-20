"""Slack Block Kit + plain-text renderings of every reply.

The approval card is rendered from the approval *database record*, never from model text, and the
buttons carry only the approval id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .approvals import Approval
from .manual_fix import TITLE as MANUAL_FIX_TITLE
from .router import Route

CONF_KO = {"low": "낮음", "medium": "중간", "high": "높음"}
APPROVE_ACTION = "aiops_approve"
REJECT_ACTION = "aiops_reject"
_SECTION_MAX = 2900


@dataclass
class Reply:
    text: str
    blocks: list[dict] | None = None
    route: str = ""
    approval_id: str | None = None
    meta: dict = field(default_factory=dict)


def esc(text: str | None) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADING = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)


def to_mrkdwn(text: str | None) -> str:
    """Model output often uses Markdown; Slack mrkdwn uses *bold* and has no headings."""
    out = _MD_BOLD.sub(r"*\1*", text or "")
    return _MD_HEADING.sub(r"*\1*", out)


def section(text: str) -> dict:
    if len(text) > _SECTION_MAX:
        text = text[: _SECTION_MAX - 20] + "\n…(생략)"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:2900]}]}


def bullets(items: list[str], limit: int = 8) -> str:
    items = [i for i in items if i]
    if not items:
        return "• (없음)"
    out = [f"• {i}" for i in items[:limit]]
    if len(items) > limit:
        out.append(f"• …외 {len(items) - limit}건")
    return "\n".join(out)


def route_line(route: Route) -> str:
    label = {"investigate": "에이전트(조사)", "change_request": "에이전트(변경 제안만)", "cost_report": "워크플로(비용 리포트)",
             "alarm_report": "워크플로(알람 현황)", "policy_forbid": "정책 차단(모델 호출 없음)",
             "approval_text": "승인 문구 무시(모델 호출 없음)", "help": "도움말"}.get(route.kind, route.kind)
    return f"경로: {label} · 규칙 `{route.rule}`"


# --------------------------------------------------------------------------- simple replies
def reply_help(route: Route) -> Reply:
    text = ("*사내 운영 에이전트 (읽기 전용 + runbook 제안)*\n"
            "• 조사: `checkout-api 5xx 늘었어, 원인 봐줘`\n"
            "• 리포트(고정 워크플로): `어제 대비 비용이 튄 서비스는?`, `알람 현황`\n"
            "• 변경 요청: `재시작해줘` → 허용 목록 runbook을 *제안*만 하고, 승인자가 버튼을 눌러야 실행됩니다.\n"
            "• 맞는 runbook이 없으면 AWS 공식 문서에 근거한 *수동 조치 초안*(검증 필요)을 드립니다. 실행은 사람이 합니다.\n"
            "• 같은 스레드에서 이어서 물으면 조사 노트를 이어 씁니다.")
    return Reply(text=text, blocks=[section(text), context(route_line(route))], route=route.kind)


def reply_policy(route: Route) -> Reply:
    text = (f"*요청을 처리하지 않았습니다.* {esc(route.reason)}\n"
            "이 에이전트는 허용 목록(runbook)에 있는 변경만 *제안*할 수 있고, 이런 변경을 할 도구도 IAM 권한도 없습니다.\n"
            "꼭 필요하면 변경 관리 절차(티켓 + 보안 검토)로 요청해 주세요.")
    return Reply(text=text, blocks=[section(text), context(route_line(route) + " · 감사 로그에 기록됨")], route=route.kind)


def reply_approval_text(route: Route) -> Reply:
    text = ("*채팅 문구로는 승인되지 않습니다.* 승인은 승인 카드의 [승인] 버튼으로만 처리되고, "
            "버튼을 누른 사람이 승인자 목록에 있는지 서버가 확인합니다. (승인 상태는 모델이나 대화 기록에 두지 않습니다.)")
    return Reply(text=text, blocks=[section(text), context(route_line(route))], route=route.kind)


def reply_busy() -> Reply:
    text = "이 스레드에서 이전 요청을 처리하고 있습니다. 끝난 뒤 다시 요청해 주세요. (스레드당 한 번에 하나)"
    return Reply(text=text, blocks=[section(text)], route="busy")


def reply_error(message: str, route: Route | None = None) -> Reply:
    text = f"*처리 중 오류가 났습니다.* {esc(message)[:800]}"
    blocks = [section(text)]
    if route:
        blocks.append(context(route_line(route)))
    return Reply(text=text, blocks=blocks, route=route.kind if route else "error")


# --------------------------------------------------------------------------- workflow
def reply_workflow(wf, route: Route) -> Reply:
    meta_line = (f"{route_line(route)} · 요약 출처: {'LLM(게이트 통과)' if wf.summary_source == 'llm' else '템플릿'} · "
                 f"근거 {', '.join(wf.evidence_ids) or '-'}" + (f" · ${wf.cost_usd:.3f}" if wf.cost_usd else ""))
    gate = " / ".join(wf.gate_notes)
    text = f"*{wf.title}*\n{wf.summary}\n```\n{wf.table}\n```\n_{gate}_"
    blocks = [section(f"*{esc(wf.title)}*\n{esc(wf.summary)}")]
    if wf.table:
        blocks.append(section(f"```\n{esc(wf.table)}\n```"))
    blocks.append(context(esc(gate)))
    blocks.append(context(meta_line))
    return Reply(text=text + "\n" + meta_line, blocks=blocks, route=route.kind,
                 meta={"evidence": wf.evidence_ids, "summary_source": wf.summary_source, "cost_usd": wf.cost_usd})


# --------------------------------------------------------------------------- manual remediation draft
_PHASE_KO = {"precheck": "사전 확인 (읽기 전용)", "change": "변경", "verification": "검증 방법 (읽기 전용)",
             "rollback": "되돌리기"}


def _doc_refs(fix: dict) -> dict[str, int]:
    return {u: n for n, u in enumerate(fix.get("doc_urls", []), 1)}


def _item_lines(item: dict, refs: dict[str, int], slack: bool) -> str:
    warn = "⚠️ " if item.get("destructive") else ""
    tag = "읽기 전용" if item.get("read_only") else "변경"
    cites = ", ".join(f"[{refs[u]}]" for u in item.get("doc_urls", []) if u in refs)
    head = f"{warn}{item['ref']} {item['purpose']} ({tag}) 근거 {cites}"
    lines = [f"*{esc(head)}*" if slack else head]
    if item.get("command"):
        lines.append(f"```{esc(item['command'])}```" if slack else f"    $ {item['command']}")
    if item.get("console_path"):
        lines.append(("콘솔: " + esc(item["console_path"])) if slack else f"    콘솔: {item['console_path']}")
    if item.get("risk_note"):
        lines.append(("주의: " + esc(item["risk_note"])) if slack else f"    주의: {item['risk_note']}")
    return "\n".join(lines)


def manual_fix_render(mfix) -> tuple[list[dict], str]:
    """Separate block: '수동 조치 초안 · 검증 필요 (실행은 사람이)'. `mfix` = agent.ManualFix or None."""
    if mfix is None:
        return [], ""
    fix = mfix.fix or {}
    refs = _doc_refs(fix)
    title = MANUAL_FIX_TITLE
    groups = [("precheck", [i for i in fix.get("steps", []) if i.get("phase") == "precheck"]),
              ("change", [i for i in fix.get("steps", []) if i.get("phase") == "change"]),
              ("verification", fix.get("verification", [])), ("rollback", fix.get("rollback", []))]
    blocks: list[dict] = [{"type": "divider"}, {"type": "header", "text": {"type": "plain_text", "text": title[:150]}}]
    text_lines = ["", "=" * 8 + f" {MANUAL_FIX_TITLE} " + "=" * 8]
    intro = []
    if mfix.error:
        intro.append(f"초안을 만들지 못했습니다: {mfix.error[:300]}")
    elif not mfix.has_steps:
        intro.append("근거 문서로 뒷받침되는 단계가 없어 초안을 표시하지 않습니다. 담당 팀이 직접 판단해 주세요.")
        if fix.get("not_applicable_reason"):
            intro.append(f"사유: {fix['not_applicable_reason']}")
    else:
        if fix.get("summary"):
            intro.append(fix["summary"])
        intro.append("이 에이전트는 아래 명령을 실행하지 않습니다(실행 도구 없음). 사람이 문서와 대조한 뒤 직접 실행하세요. "
                     "⚠️ = 삭제·보안그룹·IAM·중지 등 영향이 큰 변경.")
    if fix.get("covered_by_runbook"):
        intro.append(f"승인된 runbook으로 할 수 있는 조치: `{fix['covered_by_runbook']}` (제안 → 승인 → 실행 경로 사용)")
    blocks.append(section(esc("\n".join(intro))))
    text_lines += intro
    if mfix.has_steps:
        for n, (phase, items) in enumerate([(p, i) for p, i in groups if i], 1):
            label = f"{n}. {_PHASE_KO[phase]}"
            body = "\n".join(_item_lines(i, refs, slack=True) for i in items)
            blocks.append(section(f"*{label}*\n{body}"))
            text_lines += ["", f"[{label}]"] + [_item_lines(i, refs, slack=False) for i in items]
        docs = [f"[{n}] <{u}|{esc(u.split('://', 1)[-1][:90])}>" for u, n in refs.items()]
        blocks.append(section("*근거 문서* (AWS Knowledge MCP가 실제로 돌려준 URL만)\n" + "\n".join(docs)))
        text_lines += ["", "[근거 문서]"] + [f"[{n}] {u}" for u, n in refs.items()]
    notes = []
    if fix.get("reviewer_note"):
        notes.append(f"독립 검토자: {fix['reviewer_note']}")
    if mfix.issues:
        notes.append("초안 게이트: " + "; ".join(mfix.issues[:8]) + (f" …외 {len(mfix.issues) - 8}건" if len(mfix.issues) > 8 else ""))
    notes.append(f"초안 비용 ${mfix.cost_usd:.3f}")
    blocks.append(context(esc(" · ".join(notes))))
    text_lines += ["", " · ".join(notes)]
    return blocks, "\n".join(text_lines)


def manual_fix_meta(mfix) -> dict | None:
    if mfix is None:
        return None
    return {"fix": mfix.fix, "issues": mfix.issues, "error": mfix.error, "cost_usd": round(mfix.cost_usd, 4)}


# --------------------------------------------------------------------------- investigation
def reply_investigation(inv, route: Route, elapsed_s: float, ctx) -> Reply:
    rca = inv.rca
    crit = inv.critique or {}
    conf = rca.get("confidence", "low")
    ev_lines = [f"`{e['id']}` {e['fact']}" for e in rca.get("evidence", [])]
    ruled = [f"{r['item']}: {r['reason']}" + (f" ({', '.join(r.get('evidence_ids') or [])})" if r.get("evidence_ids") else "")
             for r in rca.get("ruled_out", [])]
    nxt = list(rca.get("next_checks", []))
    missing = list(rca.get("missing_data", []))
    timeline = [f"{t.get('at')} {t.get('event')}" + (f" (`{t['evidence_id']}`)" if t.get("evidence_id") else "")
                for t in rca.get("timeline", [])]
    reviewer = crit.get("reviewer_note") or "-"
    verdict = {"pass": "통과", "revise": f"수정 요청 {len(crit.get('issues', []))}건 반영"}.get(crit.get("verdict"), "검토 실패")
    subagents = sorted({c["agent"] for c in ctx.tool_calls if c.get("agent")})
    meta_line = (f"{route_line(route)} · 도구 호출 {sum(1 for c in ctx.tool_calls if c['tool'].startswith('mcp__'))}회 · "
                 f"하위 에이전트 {len(subagents)}개({', '.join(subagents) or '-'}) · 차단 {len(ctx.hook_denials)}건 · "
                 f"{elapsed_s:.0f}초 · ${inv.cost_usd:.3f} · 노트 갱신")
    head = f"*{esc(rca.get('service') or '')} 조사 결과* — {esc(rca.get('summary'))}"
    blocks = [
        section(head),
        section(f"*가설* (확신도: *{CONF_KO.get(conf, conf)}*)\n{to_mrkdwn(esc(rca.get('hypothesis')))}"),
        section("*근거*\n" + esc(bullets(ev_lines))),
    ]
    if timeline:
        blocks.append(section("*타임라인*\n" + esc(bullets(timeline, 6))))
    blocks += [
        section("*배제한 것*\n" + esc(bullets(ruled, 5))),
        section("*다음 확인*\n" + esc(bullets(nxt + [f"(데이터 없음) {m}" for m in missing], 6))),
        context(f"독립 검토자: {verdict} · {esc(reviewer)}"
                + (f" · 게이트: {esc('; '.join(inv.gate_issues))}" if inv.gate_issues else "")),
        context(meta_line),
    ]
    fix_blocks, fix_text = manual_fix_render(inv.manual_fix)
    blocks += fix_blocks
    text = "\n".join([
        head.replace("*", ""), "",
        f"[가설] (확신도 {CONF_KO.get(conf, conf)}) {rca.get('hypothesis')}", "",
        "[근거]", bullets(ev_lines), "",
        "[타임라인]", bullets(timeline, 6), "",
        "[배제한 것]", bullets(ruled, 5), "",
        "[다음 확인]", bullets(nxt + [f"(데이터 없음) {m}" for m in missing], 6), "",
        f"[독립 검토자] {verdict} · {reviewer}",
        *([f"[게이트] {'; '.join(inv.gate_issues)}"] if inv.gate_issues else []),
        meta_line.replace("`", ""),
    ]) + ("\n" + fix_text if fix_text else "")
    return Reply(text=text, blocks=blocks, route=route.kind,
                 meta={"confidence": conf, "verdict": crit.get("verdict"), "cost_usd": inv.cost_usd,
                       "evidence": [e["id"] for e in rca.get("evidence", [])], "subagents": subagents,
                       "rca": rca, "critique": crit, "gate_issues": inv.gate_issues,
                       "manual_fix": manual_fix_meta(inv.manual_fix)})


# --------------------------------------------------------------------------- approvals
def _fmt(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%m-%d %H:%M")


def approval_card_blocks(a: Approval, runbook_title: str, risk: str, tz: ZoneInfo) -> list[dict]:
    params = ", ".join(f"{k}=`{esc(v)}`" for k, v in a.params.items())
    return [
        section(f"*변경 승인 요청* `{a.id}`\n*{esc(runbook_title)}*"),
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*SSM 문서*\n`{esc(a.document)}` (v{esc(a.document_version)})"},
            {"type": "mrkdwn", "text": f"*대상*\n{params}"},
            {"type": "mrkdwn", "text": f"*요청자*\n<@{esc(a.requested_by)}>"},
            {"type": "mrkdwn", "text": f"*위험도 / 만료*\n{esc(risk)} / {_fmt(a.expires_at, tz)}"},
        ]},
        section(f"*사유*\n{esc(a.reason)}"),
        context(f"근거 {', '.join(a.evidence)} · 승인 후 실행기가 허용 목록 문서만 실행하고, runbook의 완료 조건으로 검증합니다 · "
                "승인자 목록에 없는 사용자의 클릭은 거부됩니다"),
        {"type": "actions", "block_id": f"approval:{a.id}", "elements": [
            {"type": "button", "action_id": APPROVE_ACTION, "style": "primary", "value": a.id,
             "text": {"type": "plain_text", "text": "승인"},
             "confirm": {"title": {"type": "plain_text", "text": "변경 승인"},
                         "text": {"type": "mrkdwn", "text": f"`{esc(a.document)}` {params} 를 실행합니다."},
                         "confirm": {"type": "plain_text", "text": "승인"},
                         "deny": {"type": "plain_text", "text": "취소"}}},
            {"type": "button", "action_id": REJECT_ACTION, "style": "danger", "value": a.id,
             "text": {"type": "plain_text", "text": "거절"}},
        ]},
    ]


def approval_card_text(a: Approval, runbook_title: str, risk: str, tz: ZoneInfo) -> str:
    params = ", ".join(f"{k}={v}" for k, v in a.params.items())
    return "\n".join([
        "┌─ 변경 승인 요청 " + a.id,
        f"│ {runbook_title}",
        f"│ SSM 문서: {a.document} (v{a.document_version})   대상: {params}",
        f"│ 요청자: {a.requested_by}   위험도: {risk}   만료: {_fmt(a.expires_at, tz)}",
        f"│ 사유: {a.reason}",
        f"│ 근거: {', '.join(a.evidence)}",
        f"└─ [승인]  [거절]   (CLI: python -m aiops.cli approve {a.id} --as <승인자 Slack ID>)",
    ])


NO_RUNBOOK = "허용된 runbook 없음"


def reply_change(out: dict, approval: Approval | None, runbook_title: str, risk: str, route: Route, ctx, cost: float,
                 tz: ZoneInfo, manual_fix=None) -> Reply:
    msg = out.get("message", "")
    if out.get("decision") == "no_runbook" and not approval and not msg.lstrip().startswith(NO_RUNBOOK):
        msg = f"*{NO_RUNBOOK}* · " + msg
    total = cost + (manual_fix.cost_usd if manual_fix else 0.0)
    meta_line = (f"{route_line(route)} · 도구 호출 {sum(1 for c in ctx.tool_calls if c['tool'].startswith('mcp__'))}회 · "
                 f"차단 {len(ctx.hook_denials)}건 · ${total:.3f}")
    blocks = [section(to_mrkdwn(esc(msg)))]
    text = msg
    if approval:
        blocks += [{"type": "divider"}] + approval_card_blocks(approval, runbook_title, risk, tz)
        text += "\n\n" + approval_card_text(approval, runbook_title, risk, tz)
    else:
        blocks.append(context("제안된 변경 없음 (승인 카드 없음)"))
        text += "\n\n(제안된 변경 없음)"
    fix_blocks, fix_text = manual_fix_render(manual_fix)
    blocks += fix_blocks
    if fix_text:
        text += "\n" + fix_text
    blocks.append(context(meta_line))
    return Reply(text=text + "\n" + meta_line, blocks=blocks, route=route.kind,
                 approval_id=approval.id if approval else None,
                 meta={"decision": out.get("decision"), "cost_usd": total, "message": msg,
                       "manual_fix": manual_fix_meta(manual_fix)})


def decided_card_blocks(a: Approval, runbook_title: str, risk: str, tz: ZoneInfo) -> list[dict]:
    blocks = [b for b in approval_card_blocks(a, runbook_title, risk, tz) if b.get("type") != "actions"]
    label = {"APPROVED": "승인됨", "REJECTED": "거절됨", "EXPIRED": "만료됨", "EXECUTING": "실행 중",
             "SUCCEEDED": "실행 완료", "FAILED": "실패"}.get(a.status, a.status)
    who = f" · <@{a.decided_by}> {_fmt(a.decided_at, tz)}" if a.decided_by else ""
    blocks.append(context(f"*{label}*{who}" + (f" · 실행 ID `{a.execution_id}`" if a.execution_id else "")))
    return blocks


def verify_text(a: Approval) -> str:
    v = a.verify or {}
    result = v.get("result", "-")
    label = {"PASS": "검증 통과", "FAIL": "검증 실패", "INCONCLUSIVE": "검증 불충분(데이터 부족)",
             "NOT_RUN": "실행 실패로 검증 안 함", "SKIPPED": "검증 기준 없음"}.get(result, result)
    lines = [f"*{label}* · `{a.id}` · 실행 `{a.execution_id or '-'}` ({v.get('execution_status', '-')})",
             esc(v.get("summary", ""))]
    if result == "PASS":
        lines.append("증상은 완화됐습니다. 근본 원인이 배포 결함이면 재발할 수 있으니 조사 노트의 다음 확인(롤백 등)을 진행하세요.")
    elif result in {"FAIL", "INCONCLUSIVE"}:
        lines.append("완료 조건을 못 맞췄습니다. 추가 조치는 사람이 판단합니다(자동 재시도 없음).")
    return "\n".join(x for x in lines if x)
