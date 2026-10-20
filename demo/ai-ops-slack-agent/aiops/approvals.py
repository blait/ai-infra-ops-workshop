"""Approval state machine, kept entirely outside the model.

CoreBreak lesson (research/03 §3): approval state must not live in model output or chat history.
Here the only way to move PENDING -> APPROVED is `ApprovalService.decide()`, called by the Slack
button handler (or the CLI `approve` command) with the clicking user's Slack ID. The button
carries only the approval id; document and parameters are always re-read from this database.

Storage: SQLite file in local mode (var/approvals.sqlite3). Deployed mode: DynamoDB with the same
transitions as conditional writes (aiops/aws_stores.DynamoApprovalStore) and the steps below run as
separate Lambdas in a Step Functions Standard workflow (waitForTaskToken for the human decision).

BP mapping
- One change at a time (Effective harnesses): one active approval per thread and per target.
- Human approval for critical decisions (Agentic AI Lens AGENTSEC04-BP02) + timeouts
  (AGENTREL02-BP05): approvals expire after AIOPS_APPROVAL_TTL_MINUTES.
- gather -> act -> VERIFY (Agent SDK blog): execution is not "done" until the runbook's verify
  criteria pass, checked by code, not by the model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from .audit import AuditLog
from .errors import ApprovalError
from .executor import FAILED_STATUSES, OK_STATUSES, Executor, NotAllowlisted
from .notes import NotesStore
from .runbooks import RunbookRegistry
from .tools.backends import Backend

ACTIVE = ("PENDING", "APPROVED", "EXECUTING")
TERMINAL = ("REJECTED", "EXPIRED", "SUCCEEDED", "FAILED")


class LockConflict(Exception):
    """The store refused the insert because another active change holds the thread/target lock."""
_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    channel_id TEXT,
    card_ts TEXT,
    requested_by TEXT NOT NULL,
    runbook_id TEXT NOT NULL,
    document TEXT NOT NULL,
    document_version TEXT NOT NULL,
    params_json TEXT NOT NULL,
    target_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('PENDING','APPROVED','REJECTED','EXPIRED','EXECUTING','SUCCEEDED','FAILED')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    execution_id TEXT,
    executor_principal TEXT,
    verify_json TEXT
);
CREATE INDEX IF NOT EXISTS approvals_thread ON approvals(thread_id, status);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Approval:
    id: str
    thread_id: str
    channel_id: str | None
    card_ts: str | None
    requested_by: str
    runbook_id: str
    document: str
    document_version: str
    params: dict
    reason: str
    evidence: list[str]
    status: str
    created_at: str
    expires_at: str
    decided_by: str | None
    decided_at: str | None
    execution_id: str | None
    executor_principal: str | None
    verify: dict | None

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Approval":
        names = cls.__dataclass_fields__
        return cls(**{k: d.get(k) for k in names})

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Approval":
        return cls(
            id=row["id"], thread_id=row["thread_id"], channel_id=row["channel_id"], card_ts=row["card_ts"],
            requested_by=row["requested_by"], runbook_id=row["runbook_id"], document=row["document"],
            document_version=row["document_version"], params=json.loads(row["params_json"]), reason=row["reason"],
            evidence=json.loads(row["evidence_json"]), status=row["status"], created_at=row["created_at"],
            expires_at=row["expires_at"], decided_by=row["decided_by"], decided_at=row["decided_at"],
            execution_id=row["execution_id"], executor_principal=row["executor_principal"],
            verify=json.loads(row["verify_json"]) if row["verify_json"] else None,
        )


class ApprovalStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def insert(self, rec: dict) -> None:
        cols = ",".join(rec)
        marks = ",".join("?" for _ in rec)
        with self._lock:
            self._conn.execute(f"INSERT INTO approvals ({cols}) VALUES ({marks})", tuple(rec.values()))

    def get(self, approval_id: str) -> Approval | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return Approval.from_row(row) if row else None

    def active_for_thread(self, thread_id: str) -> list[Approval]:
        q = f"SELECT * FROM approvals WHERE thread_id = ? AND status IN ({','.join('?' * len(ACTIVE))})"
        with self._lock:
            rows = self._conn.execute(q, (thread_id, *ACTIVE)).fetchall()
        return [Approval.from_row(r) for r in rows]

    def active_for_target(self, target_hash: str) -> list[Approval]:
        q = f"SELECT * FROM approvals WHERE target_hash = ? AND status IN ({','.join('?' * len(ACTIVE))})"
        with self._lock:
            rows = self._conn.execute(q, (target_hash, *ACTIVE)).fetchall()
        return [Approval.from_row(r) for r in rows]

    def list(self, thread_id: str | None = None) -> list[Approval]:
        with self._lock:
            if thread_id:
                rows = self._conn.execute("SELECT * FROM approvals WHERE thread_id = ? ORDER BY created_at",
                                          (thread_id,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM approvals ORDER BY created_at").fetchall()
        return [Approval.from_row(r) for r in rows]

    def transition(self, approval_id: str, from_states: tuple[str, ...], to_state: str, **fields) -> bool:
        """Atomic compare-and-set. Returns False if the record was not in one of from_states."""
        sets = ["status = ?"] + [f"{k} = ?" for k in fields]
        params = [to_state, *fields.values(), approval_id, *from_states]
        q = (f"UPDATE approvals SET {', '.join(sets)} WHERE id = ? "
             f"AND status IN ({','.join('?' * len(from_states))})")
        with self._lock:
            cur = self._conn.execute(q, params)
        return cur.rowcount == 1

    def set_fields(self, approval_id: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE approvals SET {sets} WHERE id = ?", (*fields.values(), approval_id))

    def set_fields_if(self, approval_id: str, statuses: tuple[str, ...], **fields) -> bool:
        """Update fields only while the record is in one of `statuses` (no status change)."""
        sets = ", ".join(f"{k} = ?" for k in fields)
        q = (f"UPDATE approvals SET {sets} WHERE id = ? AND status IN ({','.join('?' * len(statuses))})")
        with self._lock:
            cur = self._conn.execute(q, (*fields.values(), approval_id, *statuses))
        return cur.rowcount == 1


class ApprovalStoreProtocol(Protocol):
    """What ApprovalService needs from a store (SQLite here, DynamoDB in aiops/aws_stores.py)."""

    def insert(self, rec: dict) -> None: ...
    def get(self, approval_id: str) -> Approval | None: ...
    def active_for_thread(self, thread_id: str) -> list[Approval]: ...
    def active_for_target(self, target_hash: str) -> list[Approval]: ...
    def list(self, thread_id: str | None = None) -> list[Approval]: ...
    def transition(self, approval_id: str, from_states: tuple[str, ...], to_state: str, **fields) -> bool: ...
    def set_fields(self, approval_id: str, **fields) -> None: ...
    def set_fields_if(self, approval_id: str, statuses: tuple[str, ...], **fields) -> bool: ...


ProgressFn = Callable[[str], Awaitable[None]]


class ApprovalService:
    def __init__(self, *, store: ApprovalStoreProtocol, runbooks: RunbookRegistry, backend: Backend,
                 executor: Executor | None, audit: AuditLog, notes: NotesStore | Any, approvers: frozenset[str],
                 ttl_minutes: int = 30, require_distinct_approver: bool = False,
                 automation_assume_role_arn: str | None = None, verify_wait_seconds: int = 0,
                 verify_poll_seconds: float = 1.0, verify_timeout_seconds: int = 900,
                 clock: Callable[[], datetime] = _utcnow, display_tz: str = "UTC",
                 target_check: Callable[[Approval], dict] | None = None):
        self.target_check = target_check  # deployed: real instance state check in the verify step
        self.store = store
        self.runbooks = runbooks
        self.backend = backend
        self.executor = executor
        self.audit = audit
        self.notes = notes
        self.approvers = approvers
        self.ttl = timedelta(minutes=ttl_minutes)
        self.require_distinct_approver = require_distinct_approver
        self.assume_role = automation_assume_role_arn
        self.verify_wait_seconds = verify_wait_seconds
        self.verify_poll_seconds = verify_poll_seconds
        self.verify_timeout_seconds = verify_timeout_seconds
        self.clock = clock
        self.tz = ZoneInfo(display_tz)

    # ------------------------------------------------------------------ propose
    def propose(self, *, thread_id: str, channel_id: str | None, requested_by: str, runbook_id: str, params: dict,
                reason: str, evidence_ids: list[str], known_evidence: set[str] | None = None) -> Approval:
        """Validate a model proposal and store it as PENDING. Raises ApprovalError with a reason
        the model can read. Nothing is executed here.

        known_evidence: evidence ids of the thread supplied by trusted code (deployed mode: the agent
        runtime's tool wrapper sends them in the Gateway `caller` argument; the model cannot set it)."""

        def reject(code: str, message: str):
            self.audit.write("proposal_rejected", thread_id=thread_id, runbook_id=runbook_id, params=params,
                             code=code, reason=message)
            raise ApprovalError(code, message)

        rb = self.runbooks.get(runbook_id)
        if rb is None:
            reject("not_allowlisted", f"허용 목록에 없는 runbook입니다: {runbook_id!r}. runbook_list에서 고르세요.")
        if not rb.executable or rb.ssm_document not in self.runbooks.allowed_documents:
            reject("draft_runbook", f"{runbook_id}는 초안(draft)이라 실행 허용 목록에 없습니다. 사람에게 넘기세요.")
        normalized, errors = self.runbooks.validate_params(rb, params)
        if errors:
            reject("bad_params", "; ".join(errors))
        service = normalized.get("service")
        if service is not None and service not in self.backend.services():
            reject("unknown_service", f"알 수 없는 서비스: {service}")
        if "InstanceId" in normalized:
            known = {i["id"] for i in self.backend.service_instances(service or "")}
            if normalized["InstanceId"] not in known:
                reject("not_in_inventory",
                       f"{normalized['InstanceId']}는 {service} 인벤토리에 없습니다. 허용 대상: {sorted(known)}")
        if not reason or len(reason.strip()) < 10:
            reject("no_reason", "reason(근거 요약)을 10자 이상 적어야 합니다.")
        known_ev = known_evidence if known_evidence is not None else self.notes.evidence_ids(thread_id)
        if not evidence_ids:
            reject("no_evidence", "근거 evidence_id(EV-..)를 1개 이상 인용해야 합니다.")
        unknown_ev = [e for e in evidence_ids if e not in known_ev]
        if unknown_ev:
            reject("unknown_evidence", f"이 스레드에 없는 근거 ID: {unknown_ev}")
        if self.store.active_for_thread(thread_id):
            active = self.store.active_for_thread(thread_id)[0]
            reject("one_at_a_time", f"이 스레드에 진행 중인 변경이 있습니다({active.id}, {active.status}). 한 번에 하나만.")
        target_hash = hashlib.sha256(f"{rb.ssm_document}|{json.dumps(normalized, sort_keys=True)}".encode()).hexdigest()
        if self.store.active_for_target(target_hash):
            reject("target_busy", "같은 대상에 대해 진행 중인 다른 변경이 있습니다.")

        now = self.clock()
        approval_id = "apr-" + secrets.token_hex(5)
        try:
            self._insert(approval_id, thread_id, channel_id, requested_by, rb, normalized, target_hash, reason,
                         evidence_ids, now)
        except LockConflict as exc:
            reject("one_at_a_time", f"같은 스레드 또는 대상에 진행 중인 변경이 있습니다({exc}). 한 번에 하나만.")
        self.audit.write("proposal_created", thread_id=thread_id, approval_id=approval_id, runbook_id=rb.id,
                         document=rb.ssm_document, params=normalized, evidence=evidence_ids, requested_by=requested_by)
        self.notes.upsert_action(thread_id, approval_id, runbook=rb.id, document=rb.ssm_document, params=normalized,
                                 status="PENDING")
        return self.store.get(approval_id)

    def _insert(self, approval_id, thread_id, channel_id, requested_by, rb, normalized, target_hash, reason,
                evidence_ids, now) -> None:
        self.store.insert({
            "id": approval_id, "thread_id": thread_id, "channel_id": channel_id, "card_ts": None,
            "requested_by": requested_by, "runbook_id": rb.id, "document": rb.ssm_document,
            "document_version": rb.ssm_document_version, "params_json": json.dumps(normalized, sort_keys=True),
            "target_hash": target_hash, "reason": reason.strip()[:800], "evidence_json": json.dumps(evidence_ids),
            "status": "PENDING", "created_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + self.ttl).isoformat(timespec="seconds"),
        })

    def attach_card(self, approval_id: str, channel_id: str, card_ts: str) -> None:
        self.store.set_fields(approval_id, channel_id=channel_id, card_ts=card_ts)

    # ------------------------------------------------------------------ decide
    def decide(self, approval_id: str, user_id: str, decision: str, *, channel_id: str | None = None,
               card_ts: str | None = None, source: str = "slack_button") -> Approval:
        if decision not in {"approve", "reject"}:
            raise ApprovalError("bad_decision", "decision must be approve|reject")
        rec = self.store.get(approval_id)
        if rec is None:
            raise ApprovalError("not_found", f"승인 요청 {approval_id}를 찾을 수 없습니다.")

        def deny(code: str, message: str):
            self.audit.write("approval_denied", thread_id=rec.thread_id, approval_id=approval_id, user_id=user_id,
                             decision=decision, code=code, reason=message, source=source)
            raise ApprovalError(code, message)

        if channel_id and rec.channel_id and channel_id != rec.channel_id:
            deny("wrong_context", "다른 채널에서 온 승인 요청입니다.")
        if card_ts and rec.card_ts and card_ts != rec.card_ts:
            deny("wrong_context", "원래 승인 카드가 아닌 메시지에서 온 요청입니다.")
        if user_id not in self.approvers:
            deny("not_approver", f"<@{user_id}>님은 승인자 목록(AIOPS_APPROVERS)에 없습니다.")
        if self.require_distinct_approver and user_id == rec.requested_by:
            deny("self_approval", "요청자는 자기 요청을 승인할 수 없습니다.")
        if rec.status == "PENDING" and datetime.fromisoformat(rec.expires_at) < self.clock():
            self.store.transition(approval_id, ("PENDING",), "EXPIRED")
            deny("expired", "승인 대기 시간이 지나 만료됐습니다. 다시 제안을 요청하세요.")
        target = "APPROVED" if decision == "approve" else "REJECTED"
        ok = self.store.transition(approval_id, ("PENDING",), target, decided_by=user_id,
                                   decided_at=self.clock().isoformat(timespec="seconds"))
        if not ok:
            current = self.store.get(approval_id)
            deny("already_decided", f"이미 처리된 요청입니다 (상태: {current.status}, 처리자: {current.decided_by}).")
        self.audit.write("approval_decided", thread_id=rec.thread_id, approval_id=approval_id, user_id=user_id,
                         decision=target, source=source)
        self.notes.upsert_action(rec.thread_id, approval_id, status=target, decided_by=user_id)
        return self.store.get(approval_id)

    # ------------------------------------------------------------------ execute + verify
    # Three steps so the deployed Step Functions workflow can run them as separate Lambdas
    # (executor role != verify role). execute_and_verify() composes them for local mode.
    def start_execution(self, approval_id: str) -> tuple[Approval, datetime, dict]:
        """APPROVED -> EXECUTING, re-check the allowlist, start the allowlisted document only."""
        rec = self.store.get(approval_id)
        if rec is None:
            raise ApprovalError("not_found", approval_id)
        if self.executor is None:
            raise ApprovalError("not_configured", "이 실행 환경에는 실행기가 없습니다.")
        if not self.store.transition(approval_id, ("APPROVED",), "EXECUTING"):
            raise ApprovalError("not_approved", f"승인된 상태가 아닙니다 (상태: {rec.status}).")
        rb = self.runbooks.get(rec.runbook_id)
        if rb is None or not rb.executable or rec.document != rb.ssm_document \
                or rec.document not in self.runbooks.allowed_documents:
            self.store.transition(approval_id, ("EXECUTING",), "FAILED")
            self.audit.write("execution_blocked", thread_id=rec.thread_id, approval_id=approval_id, document=rec.document)
            raise ApprovalError("not_allowlisted", f"{rec.document}는 허용 목록 문서가 아닙니다. 실행하지 않았습니다.")

        started_at = self.backend.now()
        parameters = self.runbooks.ssm_parameters(rb, rec.params, self.assume_role)
        try:
            execution_id = self.executor.start(
                document=rec.document, version=rec.document_version, parameters=parameters,
                approval_id=approval_id, approved_by=rec.decided_by or "?")
        except NotAllowlisted as exc:
            self.store.transition(approval_id, ("EXECUTING",), "FAILED", verify_json=json.dumps({"error": str(exc)}))
            self.audit.write("execution_blocked", thread_id=rec.thread_id, approval_id=approval_id, error=str(exc))
            raise ApprovalError("not_allowlisted", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface AWS errors to the thread
            self.store.transition(approval_id, ("EXECUTING",), "FAILED", verify_json=json.dumps({"error": str(exc)}))
            self.audit.write("execution_error", thread_id=rec.thread_id, approval_id=approval_id, error=str(exc))
            raise ApprovalError("execution_error", f"실행 시작 실패: {exc}") from exc
        self.store.set_fields(approval_id, execution_id=execution_id, executor_principal=self.executor.principal)
        self.audit.write("execution_started", thread_id=rec.thread_id, approval_id=approval_id, document=rec.document,
                         parameters=parameters, execution_id=execution_id, principal=self.executor.principal,
                         approved_by=rec.decided_by)
        self.notes.upsert_action(rec.thread_id, approval_id, status="EXECUTING", execution_id=execution_id)
        return self.store.get(approval_id), started_at, parameters

    def execution_status(self, approval_id: str) -> dict:
        rec = self.store.get(approval_id)
        if rec is None or not rec.execution_id:
            return {"status": "Failed", "failure": "no execution id", "terminal": True}
        st = self.executor.status(rec.execution_id)
        st["terminal"] = st["status"] in OK_STATUSES | FAILED_STATUSES
        return st

    async def complete_execution(self, approval_id: str, status: dict, started_at: datetime) -> Approval:
        """Record the final execution status; on success run the deterministic verify step."""
        rec = self.store.get(approval_id)
        self.audit.write("execution_status", thread_id=rec.thread_id, approval_id=approval_id,
                         execution_id=rec.execution_id, status=status["status"], failure=status.get("failure"))
        if status["status"] not in OK_STATUSES:
            verify = {"result": "NOT_RUN", "execution_status": status["status"], "failure": status.get("failure")}
            self.store.transition(approval_id, ("EXECUTING",), "FAILED", verify_json=json.dumps(verify, ensure_ascii=False))
            self.notes.upsert_action(rec.thread_id, approval_id, status="FAILED", verify="NOT_RUN")
            return self.store.get(approval_id)
        # A real executor (SSM) does not move the mock world; let the mock backend follow the real run.
        if not getattr(self.executor, "simulates_world", False) and hasattr(self.backend, "record_restart") \
                and "InstanceId" in rec.params:
            await asyncio.to_thread(self.backend.record_restart, rec.params["InstanceId"])
        rb = self.runbooks.get(rec.runbook_id)
        verify = await self._verify(rb, rec, started_at)
        verify["execution_status"] = status["status"]
        self.store.transition(approval_id, ("EXECUTING",), "SUCCEEDED", verify_json=json.dumps(verify, ensure_ascii=False))
        self.audit.write("verify_result", thread_id=rec.thread_id, approval_id=approval_id, **verify)
        self.notes.upsert_action(rec.thread_id, approval_id, status="SUCCEEDED", verify=verify["result"],
                                 verify_detail=verify.get("summary"))
        return self.store.get(approval_id)

    async def execute_and_verify(self, approval_id: str, progress: ProgressFn | None = None) -> Approval:
        async def say(msg: str):
            if progress:
                await progress(msg)

        rec, started_at, _ = await asyncio.to_thread(self.start_execution, approval_id)
        await say(f"실행 시작: `{rec.document}` {rec.params} · 실행 ID `{rec.execution_id}` · 실행 주체 `{rec.executor_principal}`")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.verify_timeout_seconds
        status = {"status": "Pending"}
        while loop.time() < deadline:
            status = await asyncio.to_thread(self.execution_status, approval_id)
            if status.get("terminal"):
                break
            await asyncio.sleep(self.verify_poll_seconds)
        if status["status"] in OK_STATUSES:
            await say(f"실행 완료(`{status['status']}`). 완료 조건 검증 중…")
        final = await self.complete_execution(approval_id, status, started_at)
        if final.status == "FAILED":
            await say(f"실행 실패: 상태 `{status['status']}` {status.get('failure') or ''}")
        return final

    async def _verify(self, rb, rec: Approval, started_at: datetime) -> dict:
        """Deterministic completion check from the runbook's verify spec (never the model's word)."""
        if rb.verify is None:
            return {"result": "SKIPPED", "summary": "runbook에 검증 기준이 없습니다."}
        spec = rb.verify
        service = rec.params.get("service", "")
        if self.verify_wait_seconds:
            await asyncio.sleep(self.verify_wait_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.verify_timeout_seconds
        after: list = []
        while True:
            now = self.backend.now()
            points = await asyncio.to_thread(self.backend.metric_series, service, spec.metric,
                                             started_at - timedelta(minutes=spec.window_minutes), now)
            after = [(t, v) for t, v in points if t > started_at]
            if len(after) >= 2 or loop.time() >= deadline:
                break
            await asyncio.sleep(max(self.verify_poll_seconds, 1.0))
        before = [(t, v) for t, v in points if t <= started_at][-1:]
        alarms = await asyncio.to_thread(self.backend.describe_alarms, f"{service}-", None)
        watched = [a for a in alarms if any(a["name"].endswith(s) for s in spec.alarm_suffixes)]
        alarm_ok = all(a["state"] == "OK" for a in watched)
        last2 = [v for _, v in after[-2:]]
        target = await asyncio.to_thread(self.target_check, rec) if self.target_check else None
        target_ok = target is None or bool(target.get("ok"))
        if len(last2) < 2:
            result = "INCONCLUSIVE"
        else:
            result = "PASS" if all(v <= spec.max_value for v in last2) and alarm_ok and target_ok else "FAIL"
        summary = (f"{spec.metric} 실행 전 {before[0][1] if before else '?'} → 실행 후 최근 2개 {last2} "
                   f"(기준 ≤ {spec.max_value}), 알람 " + ", ".join(f"{a['name']}={a['state']}" for a in watched))
        if target is not None:
            summary += f", 대상 {target.get('summary')}"
        out = {
            "result": result, "summary": summary, "metric": spec.metric, "max": spec.max_value,
            "before": before[0][1] if before else None,
            "after": [[t.astimezone(self.tz).strftime("%H:%M"), v] for t, v in after],
            "alarms": {a["name"]: a["state"] for a in watched},
        }
        if target is not None:
            out["target"] = target
        if getattr(self.backend, "is_mock", False):
            out["data_source"] = "mock-fixtures"
        return out
