from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Awaitable, Callable

import pytest

from aiops.agent import RunResult, RunSpec
from aiops.config import PROJECT_ROOT, Settings
from aiops.pipeline import OpsPipeline
from aiops.tools import RunContext, ToolDeps, build_tools

APPROVER = "U0APPROVER"
APPROVER2 = "U0APPROVER2"
REQUESTER = "U0REQUESTER"
OUTSIDER = "U0OUTSIDER"
BAD_INSTANCE = "i-0ffffffffffffffff"
I1 = "i-0a1b2c3d4e5f60001"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        mode="mock",
        data_dir=tmp_path / "var",
        fixtures_dir=PROJECT_ROOT / "fixtures" / "checkout-incident",
        runbooks_dir=PROJECT_ROOT / "runbooks",
        workflow_llm=False,
        approvers=frozenset({APPROVER, APPROVER2}),
        verify_poll_seconds=0.0,
        verify_timeout_seconds=5,
        bedrock_region="us-east-1",
        manual_fix_draft=False,  # opt-in per test (tests/test_manual_fix.py); no-runbook change requests still draft
    )


Behavior = Callable[[RunSpec, RunContext, dict], Awaitable[RunResult]]


class FakeRunner:
    """Stands in for the model. Behaviours can call the real tool handlers to simulate tool use."""

    def __init__(self):
        self.deps: ToolDeps | None = None
        self.behaviors: dict[str, Behavior] = {}
        self.calls: list[RunSpec] = []

    def on(self, name: str, fn: Behavior) -> None:
        self.behaviors[name] = fn

    async def run(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        self.calls.append(spec)
        fn = self.behaviors.get(spec.name)
        if fn is None:
            return RunResult(name=spec.name, is_error=True, error=f"no fake behaviour for {spec.name}")
        tools = build_tools(ctx, self.deps)
        return await fn(spec, ctx, tools)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def pipeline(settings: Settings, runner: FakeRunner) -> OpsPipeline:
    p = OpsPipeline(settings, runner=runner)
    runner.deps = p.deps
    return p


async def call_tool(tools: dict, name: str, args: dict) -> dict:
    res = await tools[name].handler(args)
    return json.loads(res["content"][0]["text"]) | ({"_is_error": True} if res.get("is_error") else {})


async def seed_evidence(pipeline: OpsPipeline, thread_id: str) -> list[str]:
    """Run the read tools once for a thread (as the investigator would) and return evidence ids."""
    ctx = RunContext(thread_id=thread_id, user_id=REQUESTER, route="investigate")
    tools = build_tools(ctx, pipeline.deps)
    await call_tool(tools, "logs_error_summary", {"service": "checkout-api"})
    await call_tool(tools, "cloudwatch_metric_summary", {"service": "checkout-api", "metric": "5xx_rate"})
    return ctx.evidence_created


def settings_with(settings: Settings, **kw) -> Settings:
    return replace(settings, **kw)
