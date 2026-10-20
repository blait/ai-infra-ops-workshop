"""Command-line entry point: the same pipeline as Slack, for rehearsal and tests.

  python -m aiops.cli "checkout-api 5xx 늘었어, 원인 봐줘"            # ask (default command)
  python -m aiops.cli ask "어제 대비 비용이 튄 서비스는?" --thread demo-1
  python -m aiops.cli ask "재시작해줘" --thread demo-1                 # prints the approval card
  python -m aiops.cli approve apr-xxxx --as U0APPROVER               # same code path as the button
  python -m aiops.cli reject  apr-xxxx --as U0APPROVER
  python -m aiops.cli notes --thread demo-1 | audit --tail 30 | approvals | show-config | reset --yes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys

from dotenv import load_dotenv

from .config import PROJECT_ROOT, load_settings

COMMANDS = {"ask", "approve", "reject", "notes", "audit", "approvals", "show-config", "reset"}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m aiops.cli", description="AI-Ops agent CLI (Slack 없이 같은 파이프라인 실행)")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="질문 또는 요청 보내기")
    a.add_argument("text")
    a.add_argument("--thread", default="cli-demo", help="스레드 ID(= 세션). 같은 값이면 노트를 이어 씀")
    a.add_argument("--user", default="U0CLIUSER", help="요청자 Slack 사용자 ID 흉내")
    a.add_argument("--json", action="store_true", help="Reply 메타데이터를 JSON으로도 출력")
    for name in ("approve", "reject"):
        d = sub.add_parser(name, help=f"승인 카드의 [{'승인' if name == 'approve' else '거절'}] 버튼과 같은 경로")
        d.add_argument("approval_id")
        d.add_argument("--as", dest="user", required=True, help="버튼을 누른 사용자 ID (AIOPS_APPROVERS와 대조)")
    n = sub.add_parser("notes", help="스레드 조사 노트 출력")
    n.add_argument("--thread", default="cli-demo")
    au = sub.add_parser("audit", help="감사 로그(실행 흐름) 출력")
    au.add_argument("--thread")
    au.add_argument("--tail", type=int, default=40)
    ap = sub.add_parser("approvals", help="승인 기록 목록")
    ap.add_argument("--thread")
    sub.add_parser("show-config", help="경로별 도구·하위 에이전트·하드닝 설정 출력")
    r = sub.add_parser("reset", help="var/ 상태(승인 DB, 노트, 감사 로그, mock 시계) 초기화")
    r.add_argument("--yes", action="store_true")
    return p


async def _progress(msg: str) -> None:
    print(f"  … {msg}", file=sys.stderr, flush=True)


def _print_audit(rows: list[dict]) -> None:
    for r in rows:
        ts = r.get("ts", "")[11:19]
        keys = [k for k in ("route", "rule", "run", "tool", "agent", "decision", "evidence_id", "approval_id",
                            "document", "execution_id", "principal", "status", "result", "code", "cost_usd")
                if r.get(k) not in (None, "", [])]
        detail = " ".join(f"{k}={r[k]}" for k in keys)
        print(f"{ts} {r['event']:<22} {detail}")


async def main_async(argv: list[str]) -> int:
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["ask", *argv]
    args = _parser().parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    settings = load_settings()

    if args.cmd == "reset":
        if not args.yes:
            print("reset은 --yes가 필요합니다 (var/ 아래 승인 DB·노트·감사 로그·mock 시계를 지웁니다).")
            return 2
        if settings.data_dir.exists():
            shutil.rmtree(settings.data_dir)
        print(f"초기화: {settings.data_dir}")
        return 0

    from .errors import ApprovalError
    from .formatting import approval_card_text, verify_text
    from .pipeline import OpsPipeline

    pipeline = OpsPipeline(settings)

    if args.cmd == "ask":
        print(f"[mode={settings.mode} model={settings.model} thread={args.thread}]", file=sys.stderr)
        reply = await pipeline.handle(thread_id=args.thread, user_id=args.user, text=args.text, channel_id="cli",
                                      progress=_progress)
        print(reply.text)
        if args.json:
            print(json.dumps(reply.meta, ensure_ascii=False, indent=1, default=str))
        return 0

    if args.cmd in {"approve", "reject"}:
        try:
            rec = pipeline.decide(args.approval_id, args.user, args.cmd, channel_id="cli", source="cli")
        except ApprovalError as e:
            print(f"거부됨 [{e.code}] {e.message}")
            return 2
        title, risk = pipeline.runbook_label(rec)
        print(approval_card_text(rec, title, risk, pipeline.tz).splitlines()[0] + f" → {rec.status} by {rec.decided_by}")
        if rec.status == "APPROVED":
            try:
                rec = await pipeline.execute(rec.id, progress=_progress)
            except ApprovalError as e:
                print(f"실행 거부 [{e.code}] {e.message}")
                return 2
            print(verify_text(rec).replace("*", ""))
        return 0

    if args.cmd == "notes":
        path = pipeline.notes.thread_dir(args.thread) / "notes.md"
        print(path.read_text(encoding="utf-8") if path.exists() else f"(노트 없음: {args.thread})")
        return 0

    if args.cmd == "audit":
        _print_audit(pipeline.audit.read(thread_id=args.thread, limit=args.tail))
        return 0

    if args.cmd == "approvals":
        for a in pipeline.approvals.store.list(args.thread):
            v = (a.verify or {}).get("result", "-")
            print(f"{a.id} {a.status:<9} {a.document} {a.params} by={a.requested_by} decided={a.decided_by} "
                  f"exec={a.execution_id} verify={v}")
        return 0

    if args.cmd == "show-config":
        from . import prompts
        from .agent import RunSpec
        from .tools import READ_TOOLS, RunContext

        ctx = RunContext(thread_id="show-config", user_id="-", route="investigate")
        opts = pipeline.runner.build_options(RunSpec(
            name="investigator", system_prompt=prompts.INVESTIGATOR_SYSTEM, prompt="-", tools=list(READ_TOOLS),
            agents=prompts.SCOUTS, output_schema=prompts.RCA_SCHEMA, max_turns=settings.max_turns), ctx)
        print(json.dumps({
            "mode": settings.mode, "model": opts.model, "scout_model": settings.scout_model,
            "evaluator_model": settings.effective_evaluator_model,
            "builtin_tools": opts.tools, "allowed_tools": opts.allowed_tools, "disallowed_tools": opts.disallowed_tools,
            "subagents": {k: v.tools for k, v in (opts.agents or {}).items()}, "permission_mode": opts.permission_mode,
            "setting_sources": opts.setting_sources, "strict_mcp_config": opts.strict_mcp_config,
            "max_turns": opts.max_turns, "max_budget_usd": opts.max_budget_usd, "cwd": opts.cwd,
            "env": {k: (v if v else "<scrubbed>") for k, v in opts.env.items()},
            "allowlisted_documents": sorted(pipeline.runbooks.allowed_documents),
            "approvers": sorted(settings.approvers),
        }, ensure_ascii=False, indent=1))
        return 0
    return 1


def main() -> None:
    sys.exit(asyncio.run(main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
