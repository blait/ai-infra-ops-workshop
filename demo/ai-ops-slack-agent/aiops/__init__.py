"""AI-Ops Slack agent (workshop module 3 demo).

Layout (see README for the BP -> code map):
  router.py      deterministic routing: workflow vs agent vs change proposal
  workflows.py   fixed report workflows (prompt chaining with programmatic gates)
  agent.py       Claude Agent SDK runs: investigator + parallel scouts + separate evaluator
  tools/         in-process SDK MCP server (read-only, summarized, capped)
  guardrails.py  PreToolUse hook + SDK option hardening
  approvals.py   approval state machine outside the model (SQLite) + executor + verify
  notes.py       per-thread structured notes (compaction-friendly memory)
  pipeline.py    request orchestration shared by Slack, CLI, AgentCore entrypoint and evals
"""

__version__ = "0.1.0"
