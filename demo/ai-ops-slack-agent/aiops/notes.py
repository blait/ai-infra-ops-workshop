"""Per-thread structured notes: the agent's memory between Slack messages.

BP (Effective context engineering): "structured note-taking" + compaction. Each run starts from
a fresh model context that contains only `render_for_prompt()` (a bounded digest), never the raw
chat history. Full tool outputs are kept on disk as evidence files and pulled back just-in-time
with the `evidence_get` tool.

Files per thread (var/threads/<thread>/):
  notes.json      machine-readable notes (timeline / evidence / hypotheses / ruled_out / actions)
  notes.md        the same, rendered for humans (show it in the demo)
  evidence/EV-NN.json   capped tool output behind each evidence id
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

EVIDENCE_ID_RE = re.compile(r"EV-\d{2,4}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_thread_id(thread_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", thread_id).strip("._")
    if not cleaned:
        raise ValueError("empty thread id")
    return cleaned[:120]


def _empty(thread_id: str) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "service": None,
        "summary": None,
        "requests": [],
        "timeline": [],
        "evidence": [],
        "hypotheses": [],
        "ruled_out": [],
        "missing_data": [],
        "next_checks": [],
        "reports": [],
        "actions": [],
        "evidence_seq": 0,
    }


class NotesStore:
    def __init__(self, threads_dir: Path, display_tz: str = "Asia/Seoul"):
        self.threads_dir = threads_dir
        self.tz = ZoneInfo(display_tz)
        self._lock = threading.RLock()
        threads_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- storage
    def thread_dir(self, thread_id: str) -> Path:
        return self.threads_dir / safe_thread_id(thread_id)

    def load(self, thread_id: str) -> dict[str, Any]:
        path = self.thread_dir(thread_id) / "notes.json"
        if not path.exists():
            return _empty(thread_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, thread_id: str, notes: dict[str, Any]) -> None:
        notes["updated_at"] = _now_iso()
        d = self.thread_dir(thread_id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "notes.json.tmp"
        tmp.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(d / "notes.json")
        (d / "notes.md").write_text(self.render_markdown(notes), encoding="utf-8")

    def _mutate(self, thread_id: str, fn) -> Any:
        with self._lock:
            notes = self.load(thread_id)
            result = fn(notes)
            self.save(thread_id, notes)
            return result

    # ------------------------------------------------------------------ writes
    def add_request(self, thread_id: str, user_id: str, route: str, text: str) -> None:
        def fn(n):
            n["requests"].append({"at": _now_iso(), "user": user_id, "route": route, "text": text[:500]})
            n["requests"] = n["requests"][-30:]

        self._mutate(thread_id, fn)

    def add_evidence(self, thread_id: str, *, tool: str, args: dict, summary: str,
                     render: Callable[[str], str], agent: str | None = None) -> tuple[str, str]:
        """Allocate the next EV-NN id, render the payload with that id, store it, record a summary.

        Returns (evidence_id, payload). The payload file holds exactly what the model saw.
        """

        def fn(n):
            n["evidence_seq"] += 1
            ev_id = f"EV-{n['evidence_seq']:02d}"
            payload = render(ev_id)
            ev_dir = self.thread_dir(thread_id) / "evidence"
            ev_dir.mkdir(parents=True, exist_ok=True)
            (ev_dir / f"{ev_id}.json").write_text(
                json.dumps({"id": ev_id, "tool": tool, "args": args, "summary": summary, "agent": agent,
                            "payload": payload}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            n["evidence"].append({"id": ev_id, "tool": tool, "args": args, "summary": summary[:300],
                                  "agent": agent, "at": _now_iso()})
            return ev_id, payload

        return self._mutate(thread_id, fn)

    def get_evidence(self, thread_id: str, evidence_id: str) -> dict | None:
        if not EVIDENCE_ID_RE.fullmatch(evidence_id or ""):
            return None
        path = self.thread_dir(thread_id) / "evidence" / f"{evidence_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def evidence_ids(self, thread_id: str) -> set[str]:
        return {e["id"] for e in self.load(thread_id)["evidence"]}

    def apply_rca(self, thread_id: str, rca: dict, *, reviewer_note: str | None = None) -> None:
        """Merge a final (evaluated) RCA into the notes."""

        def fn(n):
            if rca.get("service"):
                n["service"] = rca["service"]
            n["summary"] = rca.get("summary")
            for h in n["hypotheses"]:
                if h.get("status") == "active":
                    h["status"] = "superseded"
            n["hypotheses"].append({
                "text": rca.get("hypothesis", ""),
                "confidence": rca.get("confidence", "low"),
                "status": "active",
                "evidence": [e.get("id") for e in rca.get("evidence", []) if e.get("id")],
                "reviewer_note": reviewer_note,
                "at": _now_iso(),
            })
            seen = {(r["item"]) for r in n["ruled_out"]}
            for r in rca.get("ruled_out", []):
                if r.get("item") and r["item"] not in seen:
                    n["ruled_out"].append({"item": r["item"], "reason": r.get("reason", ""),
                                           "evidence": r.get("evidence_ids", [])})
                    seen.add(r["item"])
            existing = {(t.get("at"), t.get("event")) for t in n["timeline"]}
            for t in rca.get("timeline", []):
                key = (t.get("at"), t.get("event"))
                if t.get("event") and key not in existing:
                    n["timeline"].append({"at": t.get("at"), "event": t["event"], "evidence": t.get("evidence_id")})
                    existing.add(key)
            n["timeline"].sort(key=lambda t: t.get("at") or "")
            n["missing_data"] = list(rca.get("missing_data", []))[:8]
            n["next_checks"] = list(rca.get("next_checks", []))[:8]

        self._mutate(thread_id, fn)

    def add_report(self, thread_id: str, kind: str, summary: str, evidence_ids: list[str]) -> None:
        def fn(n):
            n["reports"].append({"kind": kind, "summary": summary[:400], "evidence": evidence_ids, "at": _now_iso()})
            n["reports"] = n["reports"][-10:]

        self._mutate(thread_id, fn)

    def upsert_action(self, thread_id: str, approval_id: str, **fields: Any) -> None:
        def fn(n):
            for a in n["actions"]:
                if a["approval_id"] == approval_id:
                    a.update(fields)
                    a["updated_at"] = _now_iso()
                    return
            n["actions"].append({"approval_id": approval_id, "created_at": _now_iso(), **fields})

        self._mutate(thread_id, fn)

    # ----------------------------------------------------------------- reads
    def _fmt_time(self, iso: str | None) -> str:
        if not iso:
            return "?"
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return iso
        return dt.astimezone(self.tz).strftime("%m-%d %H:%M")

    def render_for_prompt(self, thread_id: str, max_chars: int = 3500) -> str:
        """Bounded digest given to each new model run (compaction by construction)."""
        n = self.load(thread_id)
        if not (n["evidence"] or n["hypotheses"] or n["reports"] or n["actions"]):
            return "(이 스레드의 이전 조사 기록 없음)"
        head: list[str] = [f"## 스레드 조사 노트 (thread={n['thread_id']})"]
        if n.get("service"):
            head.append(f"- 대상 서비스: {n['service']}")
        active = [h for h in n["hypotheses"] if h.get("status") == "active"]
        if active:
            h = active[-1]
            head.append(f"- 현재 가설 [{h['confidence']}]: {h['text']} (근거 {', '.join(h['evidence']) or '없음'})")
        for r in n["ruled_out"][-6:]:
            head.append(f"- 배제: {r['item']} — {r['reason']} ({', '.join(r.get('evidence') or [])})")
        for t in n["timeline"][-8:]:
            head.append(f"- 타임라인 {self._fmt_time(t.get('at'))}: {t['event']} ({t.get('evidence') or '-'})")
        for a in n["actions"][-4:]:
            head.append(f"- 조치 {a['approval_id']}: {a.get('runbook')} {a.get('params')} 상태={a.get('status')} "
                        f"검증={a.get('verify', '-')}")
        for rep in n["reports"][-3:]:
            head.append(f"- 리포트({rep['kind']}): {rep['summary']}")
        if n["next_checks"]:
            head.append("- 다음 확인 후보: " + " / ".join(n["next_checks"][:4]))
        ev_lines = [f"- {e['id']} [{e['tool']}] {e['summary']}" for e in reversed(n["evidence"])]
        out = "\n".join(head) + "\n### 근거 목록 (최신순, 상세는 evidence_get)\n"
        for line in ev_lines:
            if len(out) + len(line) + 1 > max_chars:
                out += f"- …(오래된 근거 {len(n['evidence'])} 중 일부 생략)\n"
                break
            out += line + "\n"
        return out[:max_chars]

    def render_markdown(self, n: dict[str, Any]) -> str:
        lines = [f"# 조사 노트: {n['thread_id']}", "", f"- 갱신: {self._fmt_time(n['updated_at'])} ({self.tz.key})",
                 f"- 서비스: {n.get('service') or '-'}", f"- 요약: {n.get('summary') or '-'}", "", "## 가설"]
        for h in n["hypotheses"]:
            lines.append(f"- [{h['status']}/{h['confidence']}] {h['text']} (근거 {', '.join(h['evidence'])})")
        lines += ["", "## 타임라인"]
        lines += [f"- {self._fmt_time(t.get('at'))} {t['event']} ({t.get('evidence') or '-'})" for t in n["timeline"]]
        lines += ["", "## 배제한 것"]
        lines += [f"- {r['item']}: {r['reason']} ({', '.join(r.get('evidence') or [])})" for r in n["ruled_out"]]
        lines += ["", "## 근거"]
        lines += [f"- {e['id']} `{e['tool']}` {e['summary']}" for e in n["evidence"]]
        lines += ["", "## 조치"]
        lines += [f"- {a['approval_id']} {a.get('runbook')} {a.get('params')} → {a.get('status')} / 검증 {a.get('verify', '-')}"
                  for a in n["actions"]]
        lines += ["", "## 다음 확인"] + [f"- {c}" for c in n["next_checks"]]
        lines += ["", "## 요청 기록"] + [f"- {self._fmt_time(r['at'])} {r['user']} ({r['route']}): {r['text']}"
                                       for r in n["requests"]]
        return "\n".join(lines) + "\n"
