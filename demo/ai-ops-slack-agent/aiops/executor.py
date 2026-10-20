"""Runbook executors. Only the approval service calls these; the model has no path to them.

BP (Scaling Managed Agents: decouple the brain from the hands): the executor uses its own
credentials (AIOPS_EXECUTOR_PROFILE) that are scrubbed from the agent subprocess env, and it
re-checks the allowlist itself. IAM (iam/approver-executor-policy.json) is the final boundary.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Protocol

from .tools.backends import MockState

OK_STATUSES = {"Success", "CompletedWithSuccess"}
FAILED_STATUSES = {"Failed", "TimedOut", "Cancelled", "CompletedWithFailure", "Rejected", "Exited",
                   "ChangeCalendarOverrideRejected"}
_NS = uuid.UUID("6f1c2a3e-0b4d-4e5f-8a9b-0c1d2e3f4a5b")


class NotAllowlisted(PermissionError):
    pass


def client_token(approval_id: str) -> str:
    """SSM ClientToken must be a UUID; derive it from the approval id so retries are idempotent."""
    return str(uuid.uuid5(_NS, approval_id))


class Executor(Protocol):
    principal: str

    def start(self, *, document: str, version: str, parameters: dict[str, list[str]], approval_id: str,
              approved_by: str) -> str: ...

    def status(self, execution_id: str) -> dict: ...


class MockExecutor:
    """Simulates SSM Automation. A successful AWS-RestartEC2Instance updates the mock world."""

    principal = "mock:role/aiops-runbook-executor"
    simulates_world = True  # status() itself moves the mock world (restart overlay + mock clock)

    def __init__(self, state: MockState, allowed_documents: frozenset[str], post_restart: dict | None = None,
                 now_fn=None):
        self.state = state
        self.allowed = allowed_documents
        self.post_restart = post_restart or {}
        self.now_fn = now_fn
        self.fail_documents: set[str] = set()  # tests can force failures

    def start(self, *, document, version, parameters, approval_id, approved_by) -> str:
        if document not in self.allowed:
            raise NotAllowlisted(f"document {document!r} is not allowlisted")
        exec_id = "mock-" + hashlib.sha256(approval_id.encode()).hexdigest()[:8] + "-" + client_token(approval_id)[:8]

        def fn(s):
            s["executions"].setdefault(exec_id, {"document": document, "version": version, "parameters": parameters,
                                                 "approval_id": approval_id, "approved_by": approved_by, "polls": 0})
        self.state.update(fn)
        return exec_id

    def status(self, execution_id: str) -> dict:
        def fn(s):
            ex = s["executions"].get(execution_id)
            if ex is None:
                return {"status": "Failed", "failure": "unknown execution id"}
            ex["polls"] += 1
            if ex["polls"] < 2:
                return {"status": "InProgress", "failure": None}
            if ex["document"] in self.fail_documents:
                ex["final"] = "Failed"
                return {"status": "Failed", "failure": "simulated failure"}
            if not ex.get("final"):
                ex["final"] = "Success"
                if ex["document"] == "AWS-RestartEC2Instance":
                    instance = ex["parameters"]["InstanceId"][0]
                    at = self.now_fn().strftime("%Y-%m-%dT%H:%M:%SZ") if self.now_fn else None
                    s["restarts"].append({"instance": instance, "at": at})
                    if instance == self.post_restart.get("instance"):
                        s["advance_minutes"] = s.get("advance_minutes", 0) + int(self.post_restart.get("advance_minutes", 0))
            return {"status": ex["final"], "failure": None}

        return self.state.update(fn)


class SsmExecutor:
    """Starts the allowlisted SSM Automation document with the executor's OWN credentials.

    Deployed mode: runs in the executor Lambda (role: ssm:StartAutomationExecution on the allowlisted
    document only + iam:PassRole for the automation role). `allowed_targets` is a second allowlist of
    instance ids (the one tagged demo instance); IAM on the automation role is the final boundary.
    """

    simulates_world = False

    def __init__(self, region: str, allowed_documents: frozenset[str], profile: str | None = None, session=None,
                 allowed_targets: frozenset[str] | None = None, principal: str | None = None, client=None):
        if client is None:
            import boto3

            session = session or boto3.Session(profile_name=profile, region_name=region)
            client = session.client("ssm", region_name=region)
        self.ssm = client
        self.allowed = allowed_documents
        self.allowed_targets = allowed_targets
        self.principal = principal or (f"profile:{profile}" if profile else "default-credential-chain")

    def start(self, *, document, version, parameters, approval_id, approved_by) -> str:
        if document not in self.allowed:
            raise NotAllowlisted(f"document {document!r} is not allowlisted")
        if self.allowed_targets is not None:
            targets = set(parameters.get("InstanceId", []))
            if not targets or not targets <= self.allowed_targets:
                raise NotAllowlisted(f"target {sorted(targets)} is not in the executor target allowlist")
        resp = self.ssm.start_automation_execution(
            DocumentName=document,
            DocumentVersion=version,
            Parameters=parameters,
            ClientToken=client_token(approval_id),
            Tags=[{"Key": "aiops:approval_id", "Value": approval_id},
                  {"Key": "aiops:approved_by", "Value": approved_by}],
        )
        return resp["AutomationExecutionId"]

    def status(self, execution_id: str) -> dict:
        ex = self.ssm.get_automation_execution(AutomationExecutionId=execution_id)["AutomationExecution"]
        return {"status": ex["AutomationExecutionStatus"], "failure": ex.get("FailureMessage")}
