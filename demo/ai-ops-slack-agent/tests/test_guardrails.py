import pytest

from aiops import prompts
from aiops.agent import RunSpec, SdkAgentRunner
from aiops.guardrails import BUILTIN_DENY, ToolPolicy, decide_tool_use, make_pre_tool_use_hook, subprocess_env
from aiops.tools import READ_TOOLS, RunContext, fq


@pytest.fixture
def policy():
    return ToolPolicy.build(
        "investigate", list(READ_TOOLS),
        {n: a["tools"] for n, a in prompts.SCOUTS.items()}, structured_output=True)


@pytest.fixture
def ctx():
    return RunContext(thread_id="t-guard", user_id="U1", route="investigate")


def pre(tool, tool_input=None, agent=None):
    data = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input or {}, "tool_use_id": "x",
            "session_id": "s", "transcript_path": "/dev/null", "cwd": "/tmp"}
    if agent:
        data["agent_type"] = agent
    return data


@pytest.mark.parametrize("tool", ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Read",
                                  "mcp__aiops__runbook_propose", "mcp__aiops__execute_change",
                                  "mcp__other_server__anything", "mcp__aws__call_aws"])
async def test_hook_denies_everything_not_allowlisted(policy, ctx, pipeline, tool):
    hook = make_pre_tool_use_hook(policy, ctx, pipeline.audit)
    out = await hook(pre(tool, {"command": "rm -rf /"}), "x", {"signal": None})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ctx.hook_denials and ctx.hook_denials[-1]["tool"] == tool
    assert any(r["event"] == "tool_call" and r["decision"] == "deny" for r in pipeline.audit.read())


async def test_hook_passes_route_tools_and_structured_output(policy, ctx, pipeline):
    hook = make_pre_tool_use_hook(policy, ctx, pipeline.audit)
    assert await hook(pre(fq("cloudwatch_alarm_status")), "x", {}) == {}
    assert await hook(pre("StructuredOutput", {"summary": "-"}), "x", {}) == {}


def test_subagents_are_confined_to_their_own_tools(policy):
    assert decide_tool_use(policy, pre(fq("logs_error_summary"), agent="logs-scout"))[0] == "pass"
    decision, reason, _ = decide_tool_use(policy, pre(fq("cloudtrail_recent_changes"), agent="logs-scout"))
    assert decision == "deny" and "logs-scout" in reason
    assert decide_tool_use(policy, pre(fq("logs_error_summary"), agent="rogue-agent"))[0] == "deny"


def test_agent_tool_only_for_defined_subagents_and_forced_foreground(policy):
    decision, _, updated = decide_tool_use(policy, pre("Agent", {"subagent_type": "metrics-scout", "prompt": "p",
                                                                  "run_in_background": True}))
    assert decision == "allow" and updated["run_in_background"] is False
    assert decide_tool_use(policy, pre("Agent", {"subagent_type": "general-purpose"}))[0] == "deny"
    # a subagent cannot spawn further agents
    assert decide_tool_use(policy, pre("Task", {"subagent_type": "logs-scout"}, agent="metrics-scout"))[0] == "deny"


async def test_hook_fails_closed_on_internal_error(ctx, pipeline):
    class Broken:
        route = "investigate"

        @property
        def main_tools(self):
            raise RuntimeError("boom")

        subagent_tools = {}

    hook = make_pre_tool_use_hook(Broken(), ctx, pipeline.audit)
    out = await hook(pre(fq("cloudwatch_alarm_status")), "x", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_change_route_policy_has_no_read_tools_beyond_scope():
    p = ToolPolicy.build("change_request", ["runbook_list", "runbook_get", "runbook_propose"])
    assert decide_tool_use(p, pre(fq("cost_daily_delta")))[0] == "deny"
    assert decide_tool_use(p, pre("Agent", {"subagent_type": "metrics-scout"}))[0] == "deny"


def test_sdk_options_are_hardened(pipeline, settings, ctx):
    runner = SdkAgentRunner(settings, pipeline.deps, pipeline.audit)
    opts = runner.build_options(RunSpec(name="investigator", system_prompt="s", prompt="p", tools=list(READ_TOOLS),
                                        agents=prompts.SCOUTS, output_schema=prompts.RCA_SCHEMA), ctx)
    assert opts.tools == ["Agent"]  # only built-in tool: subagent launcher
    assert set(BUILTIN_DENY) <= set(opts.disallowed_tools)
    for t in ("Bash", "Write", "Edit", "WebFetch"):
        assert t in opts.disallowed_tools
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == [] and opts.strict_mcp_config is True
    assert opts.max_turns and opts.max_budget_usd
    assert opts.verbatim_prompts is True
    assert not any(t.startswith("mcp__aiops__runbook") for t in opts.allowed_tools)  # no change tools in investigate
    assert set(opts.agents) == set(prompts.SCOUTS)
    assert opts.agents["logs-scout"].tools == [fq("logs_error_summary")]
    assert opts.hooks["PreToolUse"][0].matcher is None  # every tool goes through the hook
    no_tools = runner.build_options(RunSpec(name="evaluator", system_prompt="s", prompt="p",
                                            output_schema=prompts.CRITIQUE_SCHEMA), ctx)
    assert no_tools.tools == [] and no_tools.mcp_servers == {} and no_tools.agents is None


def test_subprocess_env_scrubs_secrets_and_selects_bedrock(settings, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    env = subprocess_env(settings)
    assert env["SLACK_BOT_TOKEN"] == "" and env["SLACK_APP_TOKEN"] == "" and env["AIOPS_EXECUTOR_PROFILE"] == ""
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1" and env["AWS_REGION"] == "us-east-1"
    assert env["CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS"] == "1"
    assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"
