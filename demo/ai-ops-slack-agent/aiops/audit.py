"""Append-only JSONL audit log: every tool call, hook decision, approval decision, execution.

Deployed mode (AIOPS_AUDIT_STDOUT=1): every record is also printed as one JSON line prefixed with
"AIOPS_AUDIT " so it lands in CloudWatch Logs (AgentCore Runtime / Lambda log groups) and can be
queried with Logs Insights (`filter @message like /AIOPS_AUDIT/`). The CLI command
`python -m aiops.cli audit` prints the local file for the "trace" scene of the demo.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MAX_FIELD_CHARS = 600


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS] + f"...(+{len(value) - _MAX_FIELD_CHARS})"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value[:50]]
    return value


class AuditLog:
    def __init__(self, path: Path | None, stdout: bool = False, component: str | None = None):
        self.path = path
        self.stdout = stdout
        self.component = component
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> dict:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event}
        if self.component:
            record["component"] = self.component
        record.update({k: _clip(v) for k, v in fields.items() if v is not None})
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            if self.stdout:
                print("AIOPS_AUDIT " + line, flush=True)
        return record

    def read(self, *, thread_id: str | None = None, limit: int | None = None) -> list[dict]:
        if self.path is None or not self.path.exists():
            return []
        rows = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if thread_id is None or rec.get("thread_id") == thread_id:
                    rows.append(rec)
        return rows[-limit:] if limit else rows
