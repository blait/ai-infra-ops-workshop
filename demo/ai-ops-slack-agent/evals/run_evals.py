"""Scenario evals with pass@k and pass^k (Demystifying evals for AI agents).

  python evals/run_evals.py                 # deterministic suite, k=3, offline (no model)
  python evals/run_evals.py --live --k 2    # + live agent suite via Bedrock (mock AWS tools)
  python evals/run_evals.py --only agent-rca-checkout-5xx --live --k 3

- Every trial runs in a fresh data dir (no state leaks between trials).
- Graders are code-based: routes, tool-call traces, approval records, structured RCA fields.
- pass@k = P(at least one of k trials passes); pass^k = P(all k pass). With n = k trials these are
  the empirical values (1 or 0 per scenario); the suite score is the mean over scenarios.
- The two suites are reported separately: deterministic code paths should have pass@k == pass^k
  (any gap is a nondeterminism bug); live agent runs are where pass^k matters.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from aiops.config import load_settings  # noqa: E402
from aiops.errors import ApprovalError  # noqa: E402
from aiops.pipeline import OpsPipeline  # noqa: E402
from aiops.router import classify  # noqa: E402
from aiops.tools import RunContext, build_tools  # noqa: E402

SCENARIOS = ROOT / "evals" / "scenarios"
RESULTS = ROOT / "evals" / "results"
APPROVERS = frozenset({"U0APPROVER", "U0APPROVER2"})
REQUESTER = "U0REQUESTER"


def comb(n: int, k: int) -> int:
    return math.comb(n, k) if 0 <= k <= n else 0


def pass_at_k(n: int, c: int, k: int) -> float:
    return 1.0 if n - c < k else 1.0 - comb(n - c, k) / comb(n, k)


def pass_pow_k(n: int, c: int, k: int) -> float:
    return comb(c, k) / comb(n, k)


class CountingRunner:
    """Wraps the real runner; in the deterministic suite any model call is a failure."""

    def __init__(self, inner, allow: bool):
        self.inner, self.allow, self.calls = inner, allow, []

    def build_options(self, *a, **kw):
        return self.inner.build_options(*a, **kw)

    async def run(self, spec, ctx):
        self.calls.append(spec.name)
        if not self.allow:
            from aiops.agent import RunResult

            return RunResult(name=spec.name, is_error=True, error="model call not allowed in deterministic suite")
        return await self.inner.run(spec, ctx)


def _contains_any(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in needles)


async def _seed(pipeline: OpsPipeline, thread: str, seed: dict | None) -> None:
    if not seed:
        return
    ctx = RunContext(thread_id=thread, user_id=REQUESTER, route="investigate")
    tools = build_tools(ctx, pipeline.deps)
    for name, args in seed.get("tools", []):
        await tools[name].handler(args)
    if seed.get("rca"):
        pipeline.notes.apply_rca(thread, seed["rca"])


def grade_reply(reply, expect: dict, pipeline: OpsPipeline, thread: str, model_calls: list[str]) -> list[str]:
    f: list[str] = []
    meta = reply.meta
    rca = meta.get("rca") or {}
    if "route" in expect and reply.route != expect["route"]:
        f.append(f"route {reply.route} != {expect['route']}")
    if expect.get("no_model_calls") and model_calls:
        f.append(f"model was called: {model_calls}")
    for needle in expect.get("text_contains_all", []):
        if needle not in reply.text:
            f.append(f"text missing {needle!r}")
    for needle in expect.get("text_not_contains", []):
        if needle in reply.text:
            f.append(f"text contains {needle!r}")
    hyp = f"{rca.get('hypothesis', '')} {rca.get('summary', '')}"
    if expect.get("hypothesis_any") and not _contains_any(hyp, expect["hypothesis_any"]):
        f.append(f"hypothesis lacks any of {expect['hypothesis_any']}: {hyp[:160]!r}")
    if expect.get("hypothesis_not_any") and _contains_any(hyp, expect["hypothesis_not_any"]):
        f.append(f"hypothesis contains forbidden term {expect['hypothesis_not_any']}")
    if expect.get("ruled_out_any"):
        ruled = " ".join(f"{r.get('item', '')} {r.get('reason', '')}" for r in rca.get("ruled_out", []))
        if not _contains_any(ruled, expect["ruled_out_any"]):
            f.append(f"ruled_out lacks any of {expect['ruled_out_any']}")
    if expect.get("evidence_valid"):
        known = pipeline.notes.evidence_ids(thread)
        cited = [e.get("id") for e in rca.get("evidence", [])]
        if not cited or any(c not in known for c in cited):
            f.append(f"invalid/no evidence citations {cited}")
    approvals = pipeline.approvals.store.list(thread)
    if "proposals_exact" in expect and len(approvals) != expect["proposals_exact"]:
        f.append(f"proposals {len(approvals)} != {expect['proposals_exact']}")
    if "proposals_max" in expect and len(approvals) > expect["proposals_max"]:
        f.append(f"proposals {len(approvals)} > {expect['proposals_max']}")
    if expect.get("proposal_document") and not any(a.document == expect["proposal_document"] for a in approvals):
        f.append(f"no proposal for {expect['proposal_document']}")
    for key in ("proposal_params", "proposal_params_if_any"):
        want = expect.get(key)
        if want and (approvals or key == "proposal_params"):
            if not any(all(a.params.get(k) == v for k, v in want.items()) for a in approvals):
                f.append(f"no proposal with params {want}: {[a.params for a in approvals]}")
    if expect.get("message_contains_any") and not _contains_any(meta.get("message", reply.text),
                                                                 expect["message_contains_any"]):
        f.append(f"message lacks any of {expect['message_contains_any']}")
    if "max_hook_denials" in expect and len(meta.get("hook_denials", [])) > expect["max_hook_denials"]:
        f.append(f"hook denials {meta.get('hook_denials')}")
    if expect.get("subagents_min") and len(meta.get("subagents", [])) < expect["subagents_min"]:
        f.append(f"subagents used {meta.get('subagents')} < {expect['subagents_min']}")
    for t in expect.get("tools_not_called", []):
        if any(c["tool"].endswith(t) for c in meta.get("tool_calls", [])):
            f.append(f"tool {t} was called")
    mfix = (meta.get("manual_fix") or {}).get("fix") or {}
    if expect.get("no_manual_fix") and (meta.get("manual_fix") or "수동 조치 초안" in reply.text):
        f.append("manual-fix draft present")
    if "manual_fix_min_steps" in expect and len(mfix.get("steps", [])) < expect["manual_fix_min_steps"]:
        f.append(f"manual-fix steps {len(mfix.get('steps', []))} < {expect['manual_fix_min_steps']}: "
                 f"{(meta.get('manual_fix') or {}).get('issues')}")
    if expect.get("manual_fix_doc_urls_grounded"):
        returned = {u for r in pipeline.audit.read() if r.get("event") == "doc_tool_result"
                    and r.get("thread_id") == thread for u in r.get("urls", [])}
        urls = mfix.get("doc_urls", [])
        if not urls or any(u not in returned for u in urls):
            f.append(f"manual-fix doc urls not all returned by the doc tool: {urls}")
        items = [i for k in ("steps", "verification", "rollback") for i in mfix.get(k, [])]
        if any(not i.get("doc_urls") for i in items):
            f.append("manual-fix item without doc url")
    return f


async def run_approval_steps(pipeline: OpsPipeline, thread: str, steps: list[dict]) -> list[str]:
    f: list[str] = []
    approval_id = None
    evidence: list[str] = []
    for i, st in enumerate(steps):
        op = st["op"]
        try:
            if op == "seed_evidence":
                ctx = RunContext(thread_id=thread, user_id=REQUESTER, route="investigate")
                await build_tools(ctx, pipeline.deps)["logs_error_summary"].handler({"service": "checkout-api"})
                evidence = ctx.evidence_created
            elif op == "propose":
                rec = pipeline.approvals.propose(thread_id=thread, channel_id="C-EVAL", requested_by=REQUESTER,
                                                 runbook_id=st["runbook_id"], params=st["params"],
                                                 reason="eval: 근거 기반 제안 사유 문장", evidence_ids=evidence)
                approval_id = rec.id
            elif op == "decide":
                rec = pipeline.decide(approval_id, st["user"], st["decision"], channel_id="C-EVAL", source="eval")
                if st.get("expect_status") and rec.status != st["expect_status"]:
                    f.append(f"step {i}: status {rec.status} != {st['expect_status']}")
            elif op == "say":
                reply = await pipeline.handle(thread_id=thread, user_id=st["user"],
                                              text=st["text"].format(approval_id=approval_id), channel_id="C-EVAL")
                if st.get("expect_route") and reply.route != st["expect_route"]:
                    f.append(f"step {i}: route {reply.route} != {st['expect_route']}")
            elif op == "execute":
                rec = await pipeline.execute(approval_id)
                if st.get("expect_status") and rec.status != st["expect_status"]:
                    f.append(f"step {i}: status {rec.status} != {st['expect_status']}")
                if st.get("expect_verify") and (rec.verify or {}).get("result") != st["expect_verify"]:
                    f.append(f"step {i}: verify {(rec.verify or {}).get('result')} != {st['expect_verify']}")
            elif op == "assert_status":
                status = pipeline.approvals.store.get(approval_id).status
                if status != st["status"]:
                    f.append(f"step {i}: status {status} != {st['status']}")
            else:
                f.append(f"step {i}: unknown op {op}")
                continue
            if st.get("expect_error"):
                f.append(f"step {i}: expected error {st['expect_error']} but succeeded")
        except ApprovalError as e:
            if st.get("expect_error") != e.code:
                f.append(f"step {i} ({op}): unexpected error {e.code}: {e.message}")
    return f


async def run_trial(scn: dict, base, live: bool) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="aiops-eval-"))
    settings = replace(base, data_dir=tmp / "var")
    pipeline = OpsPipeline(settings)
    counting = CountingRunner(pipeline.runner, allow=live and scn["suite"] == "live")
    pipeline.runner = pipeline.flows.runner = pipeline.workflows.runner = counting
    thread = f"eval-{scn['id']}"
    started = time.monotonic()
    failures: list[str] = []
    cost = 0.0
    excerpt = None
    kind = scn["kind"]
    if kind == "router":
        got = classify(scn["input"]).kind
        if got != scn["expect"]["route"]:
            failures.append(f"route {got} != {scn['expect']['route']}")
    elif kind == "tool":
        ctx = RunContext(thread_id=thread, user_id=REQUESTER, route="investigate")
        res = await build_tools(ctx, pipeline.deps)[scn["tool"]].handler(scn["args"])
        text = res["content"][0]["text"]
        exp = scn["expect"]
        failures += [f"text contains {n!r}" for n in exp.get("text_not_contains", []) if n in text]
        failures += [f"text missing {n!r}" for n in exp.get("text_contains_all", []) if n not in text]
        if exp.get("max_chars") and len(text) > exp["max_chars"]:
            failures.append(f"{len(text)} chars > {exp['max_chars']}")
    elif kind == "gate":
        from aiops.manual_fix import apply_review, gate_manual_fix

        fix, issues = gate_manual_fix(scn["draft"], {u: {"title": "", "text": ""} for u in scn["doc_hits"]},
                                      scn.get("evidence_text", ""))
        if "review" in scn:
            fix, more = apply_review(fix, scn["review"])
            issues += more
        exp = scn["expect"]
        got = {k: [i["ref"] for i in fix[k]] for k in ("steps", "verification", "rollback")}
        for k, refs in exp.get("refs", {}).items():
            if got[k] != refs:
                failures.append(f"{k} refs {got[k]} != {refs}")
        for needle in exp.get("issues_contain", []):
            if not any(needle in i for i in issues):
                failures.append(f"no gate issue containing {needle!r}: {issues}")
        if "destructive_refs" in exp:
            d = [i["ref"] for k in ("steps", "verification", "rollback") for i in fix[k] if i.get("destructive")]
            if d != exp["destructive_refs"]:
                failures.append(f"destructive {d} != {exp['destructive_refs']}")
        for needle in exp.get("commands_not_contain", []):
            if any(needle in i["command"] for k in ("steps", "verification", "rollback") for i in fix[k]):
                failures.append(f"command still contains {needle!r}")
    elif kind == "approval":
        failures += await run_approval_steps(pipeline, thread, scn["steps"])
    elif kind in {"pipeline", "agent"}:
        await _seed(pipeline, thread, scn.get("seed"))
        reply = None
        for text in scn["turns"]:
            reply = await pipeline.handle(thread_id=thread, user_id=REQUESTER, text=text, channel_id="C-EVAL")
            cost += float(reply.meta.get("cost_usd") or 0)
        failures += grade_reply(reply, scn["expect"], pipeline, thread, counting.calls)
        excerpt = {"route": reply.route, "text": reply.text[:900],
                   "proposals": [{"document": a.document, "params": a.params}
                                 for a in pipeline.approvals.store.list(thread)]}
    else:
        failures.append(f"unknown kind {kind}")
    return {"passed": not failures, "failures": failures, "cost_usd": round(cost, 4),
            "elapsed_s": round(time.monotonic() - started, 1), "model_runs": counting.calls, "reply": excerpt}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=3, help="trials per scenario")
    ap.add_argument("--live", action="store_true", help="also run the live agent suite (Bedrock)")
    ap.add_argument("--only", help="comma-separated scenario ids")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    base = replace(load_settings(), mode="mock", approvers=APPROVERS, workflow_llm=False, verify_poll_seconds=0.0,
                   verify_wait_seconds=0, verify_timeout_seconds=10)
    scenarios = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCENARIOS.glob("*.json"))]
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted]
    results = {}
    for scn in scenarios:
        if scn["suite"] == "live" and not args.live:
            results[scn["id"]] = {"suite": "live", "skipped": "needs --live (Bedrock)"}
            continue
        trials = []
        for i in range(args.k):
            trials.append(await run_trial(scn, base, args.live))
            status = "PASS" if trials[-1]["passed"] else "FAIL " + "; ".join(trials[-1]["failures"])[:200]
            print(f"  {scn['id']} trial {i + 1}/{args.k}: {status} ({trials[-1]['elapsed_s']}s, "
                  f"${trials[-1]['cost_usd']})", file=sys.stderr, flush=True)
        c = sum(t["passed"] for t in trials)
        results[scn["id"]] = {"suite": scn["suite"], "source": scn.get("source"), "n": args.k, "c": c,
                              "pass@k": pass_at_k(args.k, c, args.k), "pass^k": pass_pow_k(args.k, c, args.k),
                              "cost_usd": round(sum(t["cost_usd"] for t in trials), 4), "trials": trials}

    print(f"\n# evals (k={args.k}, mode=mock tools, model={base.model if args.live else 'none'})")
    for suite in ("deterministic", "live"):
        rows = {k: v for k, v in results.items() if v["suite"] == suite}
        if not rows:
            continue
        print(f"\n## {suite} suite")
        print(f"{'scenario':<36} {'c/n':>5} {'pass@k':>7} {'pass^k':>7} {'cost$':>7}")
        ran = {k: v for k, v in rows.items() if "skipped" not in v}
        for sid, r in rows.items():
            if "skipped" in r:
                print(f"{sid:<36} {'-':>5} {'skip':>7} {'skip':>7}   ({r['skipped']})")
            else:
                print(f"{sid:<36} {r['c']}/{r['n']:<3} {r['pass@k']:>7.2f} {r['pass^k']:>7.2f} {r['cost_usd']:>7.3f}")
        if ran:
            m_at = sum(r["pass@k"] for r in ran.values()) / len(ran)
            m_pow = sum(r["pass^k"] for r in ran.values()) / len(ran)
            trial_rate = sum(r["c"] for r in ran.values()) / sum(r["n"] for r in ran.values())
            print(f"{'mean over ' + str(len(ran)) + ' scenarios':<36} {'':>5} {m_at:>7.2f} {m_pow:>7.2f} "
                  f"{sum(r['cost_usd'] for r in ran.values()):>7.3f}   (trial pass rate {trial_rate:.2f})")
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"evals-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"k": args.k, "live": args.live, "model": base.model, "results": results},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nresults: {out.relative_to(ROOT)}")
    failed = [k for k, v in results.items() if "skipped" not in v and v["c"] < v["n"]]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
