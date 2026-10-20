"""Thread notes on Amazon Bedrock AgentCore Memory (short-term events, one session per Slack thread).

The structured-notes technique stays exactly as in local mode (aiops/notes.py: timeline / evidence /
hypotheses / ruled-out / actions, bounded digest per run). AgentCore Memory is the durable store, so
any runtime microVM (a new session VM, a redeploy) can rebuild the thread's notes:

  actorId   = "aiops"                     (the notes belong to the thread, not to one Slack user)
  sessionId = Slack thread  "slack-thread-<channel>-<thread_ts>" (dots -> "_", Memory id charset)
  events    = blob (a JSON *string*: a JSON object sent as a document comes back from ListEvents as a
              lossy Java Map.toString(), verified 2026-10-18)
              {"kind": "notes",    "seq": n, "notes": {...}}           snapshot after each request
              blob {"kind": "evidence", "seq": n, "evidence": {...}}        one per EV-NN (payload as the model saw it)
              blob {"kind": "action",   "seq": n, "approval_id", "fields"}  written by the approval Lambdas
              conversational USER / ASSISTANT turns (request text, reply text) for observability

hydrate(): latest notes snapshot + all evidence files + action events replayed in order.
persist(): new evidence + snapshot + the two conversational turns.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from .notes import EVIDENCE_ID_RE, NotesStore

ACTOR_ID = "aiops"
_MAX_TEXT = 8000


def memory_session_id(thread_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", thread_id).strip("_-") or "default"
    if not cleaned[0].isalnum():
        cleaned = "s" + cleaned
    return cleaned[:100]


def _seq() -> int:
    return time.time_ns()


class AgentCoreMemory:
    """Minimal wrapper over bedrock-agentcore CreateEvent / ListEvents."""

    def __init__(self, memory_id: str, region: str, client=None, actor_id: str = ACTOR_ID):
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client("bedrock-agentcore", region_name=region,
                                  config=Config(retries={"mode": "standard", "max_attempts": 4}))
        self.client = client
        self.memory_id = memory_id
        self.actor_id = actor_id

    def put_blob(self, thread_id: str, doc: dict, kind: str) -> str:
        resp = self.client.create_event(
            memoryId=self.memory_id, actorId=self.actor_id, sessionId=memory_session_id(thread_id),
            eventTimestamp=datetime.now(timezone.utc), payload=[{"blob": json.dumps(doc, ensure_ascii=False)}],
            metadata={"kind": {"stringValue": kind}})
        return resp.get("event", {}).get("eventId", "")

    def put_turns(self, thread_id: str, turns: list[tuple[str, str]]) -> None:
        payload = [{"conversational": {"content": {"text": text[:_MAX_TEXT] or "-"}, "role": role}}
                   for role, text in turns]
        self.client.create_event(memoryId=self.memory_id, actorId=self.actor_id,
                                 sessionId=memory_session_id(thread_id), eventTimestamp=datetime.now(timezone.utc),
                                 payload=payload, metadata={"kind": {"stringValue": "turns"}})

    def list_events(self, thread_id: str) -> list[dict]:
        out, token = [], None
        while True:
            kwargs: dict[str, Any] = dict(memoryId=self.memory_id, actorId=self.actor_id,
                                          sessionId=memory_session_id(thread_id), includePayloads=True, maxResults=100)
            if token:
                kwargs["nextToken"] = token
            try:
                resp = self.client.list_events(**kwargs)
            except Exception as exc:  # a brand-new session has no events yet
                if "ResourceNotFound" in type(exc).__name__ or "ResourceNotFound" in str(exc):
                    return out
                raise
            out += resp.get("events", [])
            token = resp.get("nextToken")
            if not token:
                return out


def _blobs(events: list[dict]) -> list[dict]:
    docs = []
    for ev in events:
        for p in ev.get("payload", []):
            doc = p.get("blob")
            if isinstance(doc, str):
                try:
                    doc = json.loads(doc)
                except json.JSONDecodeError:
                    continue
            if isinstance(doc, dict) and doc.get("kind"):
                docs.append(doc)
    return sorted(docs, key=lambda d: d.get("seq", 0))


class NotesMemorySync:
    def __init__(self, memory: AgentCoreMemory, notes: NotesStore, audit=None):
        self.memory = memory
        self.notes = notes
        self.audit = audit

    def hydrate(self, thread_id: str) -> dict:
        events = self.memory.list_events(thread_id)
        docs = _blobs(events)
        snapshots = [d for d in docs if d["kind"] == "notes" and isinstance(d.get("notes"), dict)]
        evidence = [d["evidence"] for d in docs if d["kind"] == "evidence" and isinstance(d.get("evidence"), dict)]
        actions = [d for d in docs if d["kind"] == "action" and d.get("approval_id")]
        if snapshots:
            self.notes.save(thread_id, snapshots[-1]["notes"])
        ev_dir = self.notes.thread_dir(thread_id) / "evidence"
        for ev in evidence:
            ev_id = ev.get("id", "")
            if not EVIDENCE_ID_RE.fullmatch(ev_id):
                continue
            ev_dir.mkdir(parents=True, exist_ok=True)
            (ev_dir / f"{ev_id}.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")
        for a in actions:
            self.notes.upsert_action(thread_id, a["approval_id"], **(a.get("fields") or {}))
        info = {"events": len(events), "snapshots": len(snapshots), "evidence": len(evidence), "actions": len(actions)}
        if self.audit:
            self.audit.write("memory_hydrate", thread_id=thread_id, **info)
        return info

    def persist(self, thread_id: str, *, new_evidence: list[str], request: str, reply: str, user_id: str) -> dict:
        written = 0
        for ev_id in new_evidence:
            ev = self.notes.get_evidence(thread_id, ev_id)
            if ev:
                self.memory.put_blob(thread_id, {"kind": "evidence", "seq": _seq(), "evidence": ev}, "evidence")
                written += 1
        self.memory.put_blob(thread_id, {"kind": "notes", "seq": _seq(), "notes": self.notes.load(thread_id)}, "notes")
        self.memory.put_turns(thread_id, [("USER", f"<{user_id}> {request}"), ("ASSISTANT", reply)])
        info = {"evidence_events": written, "snapshot": 1, "turns": 2}
        if self.audit:
            self.audit.write("memory_persist", thread_id=thread_id, **info)
        return info


class MemoryActionSink:
    """Notes stand-in for the approval Lambdas: action updates become Memory events that the agent
    runtime replays on its next hydrate. evidence_ids() is never trusted from here (the caller
    context carries the thread's evidence ids)."""

    def __init__(self, memory: AgentCoreMemory | None):
        self.memory = memory

    def upsert_action(self, thread_id: str, approval_id: str, **fields: Any) -> None:
        if self.memory is None:
            return
        try:
            self.memory.put_blob(thread_id, {"kind": "action", "seq": _seq(), "approval_id": approval_id,
                                             "fields": json.loads(json.dumps(fields, default=str))}, "action")
        except Exception as exc:  # noqa: BLE001 - notes are best-effort; the approval record is in DynamoDB
            print(json.dumps({"memory_action_write_failed": str(exc)[:300], "approval_id": approval_id}))

    def evidence_ids(self, thread_id: str) -> set[str]:
        return set()
