import importlib.util
import json
from dataclasses import replace

import pytest

from aiops.config import PROJECT_ROOT

spec = importlib.util.spec_from_file_location("run_evals", PROJECT_ROOT / "evals" / "run_evals.py")
run_evals = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_evals)


def test_pass_at_k_and_pass_pow_k():
    assert run_evals.pass_at_k(3, 3, 3) == 1.0 and run_evals.pass_pow_k(3, 3, 3) == 1.0
    assert run_evals.pass_at_k(3, 1, 3) == 1.0 and run_evals.pass_pow_k(3, 2, 3) == 0.0
    assert run_evals.pass_at_k(5, 1, 3) == pytest.approx(0.6)  # 1 - C(4,3)/C(5,3)
    assert run_evals.pass_pow_k(5, 4, 3) == pytest.approx(0.4)  # C(4,3)/C(5,3)
    assert run_evals.pass_at_k(3, 0, 3) == 0.0


def test_scenarios_are_well_formed():
    files = sorted((PROJECT_ROOT / "evals" / "scenarios").glob("*.json"))
    assert 10 <= len(files) <= 25
    ids = set()
    for f in files:
        s = json.loads(f.read_text(encoding="utf-8"))
        assert s["id"] == f.stem and s["id"] not in ids and s["suite"] in {"deterministic", "live"}
        assert s.get("source"), f"{s['id']}: every scenario cites where it came from"
        ids.add(s["id"])


async def test_grader_catches_a_wrong_expectation(settings):
    scn = json.loads((PROJECT_ROOT / "evals" / "scenarios" / "wf-cost-report-numbers.json").read_text())
    ok = await run_evals.run_trial(scn, settings, live=False)
    assert ok["passed"], ok["failures"]
    broken = dict(scn, expect=dict(scn["expect"], text_contains_all=["$999,999.99"], route="investigate"))
    bad = await run_evals.run_trial(broken, settings, live=False)
    assert not bad["passed"] and len(bad["failures"]) == 2


async def test_live_scenario_cannot_call_model_offline(settings):
    scn = json.loads((PROJECT_ROOT / "evals" / "scenarios" / "agent-change-no-runbook.json").read_text())
    res = await run_evals.run_trial(scn, replace(settings, approvers=run_evals.APPROVERS), live=False)
    assert res["model_runs"] == ["change"] and not res["passed"]  # blocked, so the grader fails it
