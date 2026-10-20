"""DynamoDB-backed stores for the deployed (Lambda / Step Functions) side.

One table, partition key `pk` (string), TTL attribute `ttl` (epoch seconds):

  APR#<approval_id>         the approval record (same fields as the SQLite store) + task_token
  LOCK#THREAD#<thread_id>   one active change per thread   } created in ONE transaction with the
  LOCK#TARGET#<sha256>      one active change per target   } approval (attribute_not_exists)
  EVT#<slack_event_id>      Slack event de-duplication (ttl 1 h)
  MOCKSTATE#default         mock world state for AIOPS_MODE=mock (clock offset, restarts)

State transitions are conditional writes: `#s IN (:from...)`. A double click therefore loses the
race in DynamoDB, not in application code. Locks are released when the approval reaches a
terminal state; they also carry an expiry so a crashed flow cannot block a thread forever.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from ..approvals import ACTIVE, TERMINAL, Approval, LockConflict

APPROVAL_RETENTION_S = 30 * 24 * 3600  # keep decided records 30 days (audit), then TTL cleanup
LOCK_GRACE_S = 30 * 60  # lock outlives the 30-minute approval window by the execution allowance

_COLUMNS = ("id", "thread_id", "channel_id", "card_ts", "requested_by", "runbook_id", "document",
            "document_version", "params_json", "target_hash", "reason", "evidence_json", "status", "created_at",
            "expires_at", "decided_by", "decided_at", "execution_id", "executor_principal", "verify_json")


def _ser():
    from boto3.dynamodb.types import TypeSerializer

    return TypeSerializer()


def _deser():
    from boto3.dynamodb.types import TypeDeserializer

    return TypeDeserializer()


def to_ddb(item: dict) -> dict:
    s = _ser()
    return {k: s.serialize(v) for k, v in item.items() if v is not None}


def from_ddb(item: dict) -> dict:
    d = _deser()
    return {k: d.deserialize(v) for k, v in item.items()}


def _iso_epoch(iso: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(iso).timestamp())


def approval_from_item(item: dict) -> Approval:
    get = item.get
    return Approval(
        id=get("id"), thread_id=get("thread_id"), channel_id=get("channel_id"), card_ts=get("card_ts"),
        requested_by=get("requested_by"), runbook_id=get("runbook_id"), document=get("document"),
        document_version=get("document_version"), params=json.loads(get("params_json") or "{}"),
        reason=get("reason") or "", evidence=json.loads(get("evidence_json") or "[]"), status=get("status"),
        created_at=get("created_at"), expires_at=get("expires_at"), decided_by=get("decided_by"),
        decided_at=get("decided_at"), execution_id=get("execution_id"),
        executor_principal=get("executor_principal"),
        verify=json.loads(get("verify_json")) if get("verify_json") else None,
    )


def _is_conditional_failure(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in {"ConditionalCheckFailedException", "TransactionCanceledException"}


class DynamoApprovalStore:
    def __init__(self, table: str, client=None, region: str | None = None, clock: Callable[[], float] = time.time):
        if client is None:
            import boto3

            client = boto3.client("dynamodb", region_name=region)
        self.ddb = client
        self.table = table
        self.clock = clock

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _key(pk: str) -> dict:
        return {"pk": {"S": pk}}

    def _get(self, pk: str) -> dict | None:
        resp = self.ddb.get_item(TableName=self.table, Key=self._key(pk), ConsistentRead=True)
        return from_ddb(resp["Item"]) if "Item" in resp else None

    # ------------------------------------------------------------------ store protocol
    def insert(self, rec: dict) -> None:
        now = int(self.clock())
        lock_exp = _iso_epoch(rec["expires_at"]) + LOCK_GRACE_S
        apr = {"pk": f"APR#{rec['id']}", "type": "approval", **rec, "ttl": now + APPROVAL_RETENTION_S}
        cond_lock = "attribute_not_exists(pk) OR lock_expires < :now"
        items = [
            {"Put": {"TableName": self.table, "Item": to_ddb(apr), "ConditionExpression": "attribute_not_exists(pk)"}},
        ]
        for kind, value in (("THREAD", rec["thread_id"]), ("TARGET", rec["target_hash"])):
            items.append({"Put": {
                "TableName": self.table,
                "Item": to_ddb({"pk": f"LOCK#{kind}#{value}", "type": "lock", "approval_id": rec["id"],
                                "lock_expires": lock_exp, "ttl": lock_exp}),
                "ConditionExpression": cond_lock,
                "ExpressionAttributeValues": {":now": {"N": str(now)}},
            }})
        for attempt in range(2):
            try:
                self.ddb.transact_write_items(TransactItems=items)
                return
            except Exception as exc:  # noqa: BLE001
                if not _is_conditional_failure(exc):
                    raise
                if attempt == 0 and self._release_stale_locks(rec):
                    continue
                raise LockConflict("thread/target lock held") from exc

    def _release_stale_locks(self, rec: dict) -> bool:
        """A lock whose approval is already terminal (crash between update and release) is stale."""
        released = False
        for kind, value in (("THREAD", rec["thread_id"]), ("TARGET", rec["target_hash"])):
            lock = self._get(f"LOCK#{kind}#{value}")
            if not lock:
                continue
            holder = self.get(lock.get("approval_id", ""))
            if holder is None or holder.status in TERMINAL:
                self._delete_lock(f"LOCK#{kind}#{value}", lock.get("approval_id", ""))
                released = True
        return released

    def _delete_lock(self, pk: str, approval_id: str) -> None:
        try:
            self.ddb.delete_item(TableName=self.table, Key=self._key(pk), ConditionExpression="approval_id = :a",
                                 ExpressionAttributeValues={":a": {"S": approval_id}})
        except Exception as exc:  # noqa: BLE001
            if not _is_conditional_failure(exc):
                raise

    def get(self, approval_id: str) -> Approval | None:
        if not approval_id:
            return None
        item = self._get(f"APR#{approval_id}")
        return approval_from_item(item) if item else None

    def get_item(self, approval_id: str) -> dict | None:
        """Raw item (includes task_token / sfn_execution_arn)."""
        return self._get(f"APR#{approval_id}")

    def _active_via_lock(self, pk: str) -> list[Approval]:
        lock = self._get(pk)
        if not lock or int(lock.get("lock_expires", 0)) < int(self.clock()):
            return []
        rec = self.get(lock.get("approval_id", ""))
        return [rec] if rec and rec.status in ACTIVE else []

    def active_for_thread(self, thread_id: str) -> list[Approval]:
        return self._active_via_lock(f"LOCK#THREAD#{thread_id}")

    def active_for_target(self, target_hash: str) -> list[Approval]:
        return self._active_via_lock(f"LOCK#TARGET#{target_hash}")

    def list(self, thread_id: str | None = None) -> list[Approval]:
        out, start = [], None
        while True:
            kwargs: dict[str, Any] = dict(TableName=self.table, FilterExpression="#t = :t",
                                          ExpressionAttributeNames={"#t": "type"},
                                          ExpressionAttributeValues={":t": {"S": "approval"}})
            if start:
                kwargs["ExclusiveStartKey"] = start
            resp = self.ddb.scan(**kwargs)
            out += [approval_from_item(from_ddb(i)) for i in resp.get("Items", [])]
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
        if thread_id:
            out = [a for a in out if a.thread_id == thread_id]
        return sorted(out, key=lambda a: a.created_at)

    def _update(self, approval_id: str, fields: dict, condition: str, values: dict, names: dict) -> bool:
        sets, s = [], _ser()
        for i, (k, v) in enumerate(fields.items()):
            names[f"#f{i}"] = k
            values[f":f{i}"] = s.serialize(v)
            sets.append(f"#f{i} = :f{i}")
        try:
            self.ddb.update_item(TableName=self.table, Key=self._key(f"APR#{approval_id}"),
                                 UpdateExpression="SET " + ", ".join(sets), ConditionExpression=condition,
                                 ExpressionAttributeNames=names, ExpressionAttributeValues=values)
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_conditional_failure(exc):
                return False
            raise

    def _status_condition(self, statuses: tuple[str, ...]) -> tuple[str, dict, dict]:
        values = {f":s{i}": {"S": st} for i, st in enumerate(statuses)}
        return (f"attribute_exists(pk) AND #s IN ({', '.join(values)})", values, {"#s": "status"})

    def transition(self, approval_id: str, from_states: tuple[str, ...], to_state: str, **fields) -> bool:
        cond, values, names = self._status_condition(from_states)
        ok = self._update(approval_id, {"status": to_state, **fields}, cond, values, names)
        if ok and to_state in TERMINAL:
            item = self._get(f"APR#{approval_id}") or {}
            self._delete_lock(f"LOCK#THREAD#{item.get('thread_id', '')}", approval_id)
            self._delete_lock(f"LOCK#TARGET#{item.get('target_hash', '')}", approval_id)
        return ok

    def set_fields(self, approval_id: str, **fields) -> None:
        self._update(approval_id, fields, "attribute_exists(pk)", {}, {})

    def set_fields_if(self, approval_id: str, statuses: tuple[str, ...], **fields) -> bool:
        cond, values, names = self._status_condition(statuses)
        return self._update(approval_id, fields, cond, values, names)

    # ------------------------------------------------------------------ Slack event de-duplication
    def claim_event(self, event_id: str, ttl_s: int = 3600) -> bool:
        now = int(self.clock())
        try:
            self.ddb.put_item(TableName=self.table, Item=to_ddb({"pk": f"EVT#{event_id}", "type": "event",
                                                                  "ttl": now + ttl_s}),
                              ConditionExpression="attribute_not_exists(pk)")
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_conditional_failure(exc):
                return False
            raise


class DynamoMockState:
    """MockState protocol (load/update/reset) on one DynamoDB item, optimistic concurrency."""

    def __init__(self, table: str, client=None, region: str | None = None, key: str = "MOCKSTATE#default"):
        if client is None:
            import boto3

            client = boto3.client("dynamodb", region_name=region)
        self.ddb = client
        self.table = table
        self.key = key

    def _read(self) -> tuple[dict, int]:
        resp = self.ddb.get_item(TableName=self.table, Key={"pk": {"S": self.key}}, ConsistentRead=True)
        if "Item" not in resp:
            return {"advance_minutes": 0, "restarts": [], "executions": {}}, 0
        item = from_ddb(resp["Item"])
        return json.loads(item.get("state_json") or "{}"), int(item.get("version", 0))

    def load(self) -> dict:
        return self._read()[0]

    def update(self, fn) -> Any:
        for _ in range(5):
            state, version = self._read()
            result = fn(state)
            cond = "attribute_not_exists(pk)" if version == 0 else "version = :v"
            kwargs: dict[str, Any] = dict(
                TableName=self.table, ConditionExpression=cond,
                Item=to_ddb({"pk": self.key, "type": "mockstate", "state_json": json.dumps(state, ensure_ascii=False),
                             "version": version + 1}))
            if version:
                kwargs["ExpressionAttributeValues"] = {":v": {"N": str(version)}}
            try:
                self.ddb.put_item(**kwargs)
                return result
            except Exception as exc:  # noqa: BLE001
                if not _is_conditional_failure(exc):
                    raise
        raise RuntimeError("mock state update kept conflicting")

    def reset(self) -> None:
        self.ddb.delete_item(TableName=self.table, Key={"pk": {"S": self.key}})
