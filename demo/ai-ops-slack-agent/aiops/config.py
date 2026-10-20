"""Settings loaded from environment variables (.env supported by the entry points).

Nothing secret has a default. Approval-related settings (approver allowlist) live here, outside
the model: the model can neither read nor change them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "global.anthropic.claude-sonnet-5-5"

# Environment variables that must never reach the Claude Code subprocess (the model harness).
# The subprocess inherits os.environ; we blank these out in ClaudeAgentOptions.env.
SCRUBBED_ENV_VARS = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    "AIOPS_EXECUTOR_PROFILE",
    "AIOPS_EXECUTOR_ROLE_ARN",
    "AIOPS_AUTOMATION_ASSUME_ROLE_ARN",
    "AIOPS_SLACK_SECRET_ARN",
)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw is not None else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw is not None else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_set(name: str) -> frozenset[str]:
    raw = _env(name, "") or ""
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    mode: Literal["mock", "aws"] = "mock"
    data_dir: Path = PROJECT_ROOT / "var"
    fixtures_dir: Path = PROJECT_ROOT / "fixtures" / "checkout-incident"
    runbooks_dir: Path = PROJECT_ROOT / "runbooks"
    services_file: Path = PROJECT_ROOT / "config" / "services.toml"
    display_tz: str = "Asia/Seoul"

    # Model (Amazon Bedrock via Claude Code: CLAUDE_CODE_USE_BEDROCK=1 + AWS_REGION)
    model: str = DEFAULT_MODEL
    scout_model: str = "inherit"
    evaluator_model: str | None = None  # None -> same as model
    use_bedrock: bool = True
    bedrock_region: str | None = None
    workflow_llm: bool = True  # LLM summary step in fixed workflows (gated); False -> template only

    # Agent loop limits
    max_turns: int = 16
    max_budget_usd: float = 1.50
    tool_output_max_chars: int = 3500
    log_row_cap: int = 200
    max_concurrent_subagents: int = 3

    # Manual remediation draft (aiops/manual_fix.py): grounded in AWS docs, executed by a human
    manual_fix_draft: bool = True  # also draft for every investigation (no-runbook change requests always draft)
    docs_mcp_url: str | None = "https://knowledge-mcp.global.api.aws"  # local mode: AWS Knowledge MCP, no auth
    docs_gateway_target: str | None = None  # deployed mode: Gateway MCP-server target name (e.g. "awsdocs")

    # AWS mode
    target_region: str | None = None  # region of the workload being investigated
    executor_profile: str | None = None  # separate credentials for SSM StartAutomationExecution
    automation_assume_role_arn: str | None = None

    # Deployed mode (all optional; unset = local mode). See infra/ and README §2.
    gateway_url: str | None = None  # AgentCore Gateway MCP URL -> tools run behind Gateway + Cedar policy
    gateway_targets: str | None = None  # "readtools:tool1|tool2;changeproposal:runbook_propose"
    memory_id: str | None = None  # AgentCore Memory id -> thread notes/evidence persisted as events
    mock_instance_map: dict = field(default_factory=dict)  # fixture instance id -> real demo instance id
    audit_stdout: bool = False  # also print audit records as JSON lines (CloudWatch Logs)

    # Approval (outside the model)
    approvers: frozenset[str] = field(default_factory=frozenset)
    require_distinct_approver: bool = False
    approval_ttl_minutes: int = 30

    # Verify step after execution
    verify_wait_seconds: int = 0
    verify_poll_seconds: float = 1.0
    verify_timeout_seconds: int = 900

    @property
    def effective_evaluator_model(self) -> str:
        return self.evaluator_model or self.model

    @property
    def threads_dir(self) -> Path:
        return self.data_dir / "threads"

    @property
    def approvals_db(self) -> Path:
        return self.data_dir / "approvals.sqlite3"

    @property
    def audit_log(self) -> Path:
        return self.data_dir / "audit.jsonl"

    @property
    def mock_state_file(self) -> Path:
        return self.data_dir / "mock_state.json"

    @property
    def agent_cwd(self) -> Path:
        """Empty working directory for the Claude Code subprocess (no repo files in reach)."""
        return self.data_dir / "agent-cwd"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.threads_dir, self.agent_cwd):
            path.mkdir(parents=True, exist_ok=True)


def _instance_map(raw: str | None) -> dict:
    from .tools.backends import parse_instance_map

    return parse_instance_map(raw)


def load_settings(**overrides) -> Settings:
    """Build Settings from env vars, then apply explicit overrides (used by tests/evals)."""
    mode = (_env("AIOPS_MODE", "mock") or "mock").lower()
    if mode not in {"mock", "aws"}:
        raise ValueError(f"AIOPS_MODE must be 'mock' or 'aws', got {mode!r}")
    data_dir = Path(_env("AIOPS_DATA_DIR", str(PROJECT_ROOT / "var")))
    values = dict(
        mode=mode,
        data_dir=data_dir,
        fixtures_dir=Path(_env("AIOPS_FIXTURES_DIR", str(PROJECT_ROOT / "fixtures" / "checkout-incident"))),
        runbooks_dir=Path(_env("AIOPS_RUNBOOKS_DIR", str(PROJECT_ROOT / "runbooks"))),
        services_file=Path(_env("AIOPS_SERVICES_FILE", str(PROJECT_ROOT / "config" / "services.toml"))),
        display_tz=_env("AIOPS_DISPLAY_TZ", "Asia/Seoul"),
        model=_env("AIOPS_MODEL") or _env("ANTHROPIC_MODEL") or DEFAULT_MODEL,
        scout_model=_env("AIOPS_SCOUT_MODEL", "inherit"),
        evaluator_model=_env("AIOPS_EVALUATOR_MODEL"),
        use_bedrock=_env_bool("CLAUDE_CODE_USE_BEDROCK", True),
        bedrock_region=_env("AWS_REGION") or _env("AWS_DEFAULT_REGION"),
        workflow_llm=_env_bool("AIOPS_WORKFLOW_LLM", True),
        max_turns=_env_int("AIOPS_MAX_TURNS", 16),
        manual_fix_draft=_env_bool("AIOPS_MANUAL_FIX_DRAFT", True),
        docs_mcp_url=_env("AIOPS_DOCS_MCP_URL", "https://knowledge-mcp.global.api.aws"),
        docs_gateway_target=_env("AIOPS_DOCS_GATEWAY_TARGET"),
        max_budget_usd=_env_float("AIOPS_MAX_BUDGET_USD", 1.50),
        tool_output_max_chars=_env_int("AIOPS_TOOL_OUTPUT_MAX_CHARS", 3500),
        log_row_cap=_env_int("AIOPS_LOG_ROW_CAP", 200),
        target_region=_env("AIOPS_TARGET_REGION"),
        executor_profile=_env("AIOPS_EXECUTOR_PROFILE"),
        automation_assume_role_arn=_env("AIOPS_AUTOMATION_ASSUME_ROLE_ARN"),
        approvers=_env_set("AIOPS_APPROVERS"),
        require_distinct_approver=_env_bool("AIOPS_REQUIRE_DISTINCT_APPROVER", False),
        approval_ttl_minutes=_env_int("AIOPS_APPROVAL_TTL_MINUTES", 30),
        verify_wait_seconds=_env_int("AIOPS_VERIFY_WAIT_SECONDS", 0 if mode == "mock" else 300),
        verify_poll_seconds=_env_float("AIOPS_VERIFY_POLL_SECONDS", 1.0 if mode == "mock" else 15.0),
        verify_timeout_seconds=_env_int("AIOPS_VERIFY_TIMEOUT_SECONDS", 900),
        gateway_url=_env("AIOPS_GATEWAY_URL"),
        gateway_targets=_env("AIOPS_GATEWAY_TARGETS"),
        memory_id=_env("AIOPS_MEMORY_ID"),
        mock_instance_map=_instance_map(_env("AIOPS_MOCK_INSTANCE_MAP")),
        audit_stdout=_env_bool("AIOPS_AUDIT_STDOUT", False),
    )
    values.update(overrides)
    settings = Settings(**values)
    if settings.mode == "aws" and not settings.target_region:
        raise ValueError("AIOPS_MODE=aws requires AIOPS_TARGET_REGION (region of the workload)")
    return settings
