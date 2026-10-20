"""Manual remediation DRAFT ("수동 조치 초안 · 검증 필요"): schema, code gates, reviewer application.

Owner's decision (2026-10-19): when no allowlisted runbook fits, and as part of every investigation
result, the agent writes a draft of concrete steps (AWS CLI and/or console) grounded in official AWS
documentation. A HUMAN executes it. Nothing here runs a command; no tool can.

Pipeline (aiops/agent.Flows.draft_manual_fix)
  drafter (tools: aws_docs_search / aws_docs_read / evidence_get only)
    -> gate_manual_fix  (code)  every item must cite a URL the doc tool actually returned in THIS run;
                                URLs the model invented are dropped, items left without one are removed.
                                Also: forbidden patterns removed, secrets redacted, resource IDs not seen
                                in evidence replaced by placeholders, read-only re-derived from the verb,
                                destructive commands flagged, order forced (precheck -> change).
    -> reviewer (NO tools) checks each item against the cited doc snippets + evidence
    -> apply_review (code)      ungrounded -> "근거 없음 → 삭제"; items the reviewer did not judge are
                                removed too; destructive flags can only be added, never cleared.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .docs import norm_url

TITLE = "수동 조치 초안 · 검증 필요 (실행은 사람이)"
SECTIONS = (("steps", "S"), ("verification", "V"), ("rollback", "R"))

_ITEM = {
    "type": "object",
    "properties": {
        "purpose": {"type": "string", "description": "이 단계의 목적, 한 줄"},
        "command": {"type": "string", "description": "AWS CLI 명령 1개(없으면 빈 문자열). 값은 근거에 있던 것만, 나머지는 <instance-id> 같은 자리표시자"},
        "console_path": {"type": "string", "description": "콘솔 경로(예: RDS > Parameter groups > ... > Edit). 없으면 빈 문자열"},
        "read_only": {"type": "boolean", "description": "조회만 하면 true"},
        "risk_note": {"type": "string", "description": "위험·주의 한 줄(영향 범위, 재시작 필요 여부 등)"},
        "doc_urls": {"type": "array", "items": {"type": "string"},
                     "description": "이 단계의 근거: aws_docs_search/aws_docs_read가 돌려준 url만"},
    },
    "required": ["purpose", "read_only", "risk_note", "doc_urls"],
}

MANUAL_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "applicable": {"type": "boolean", "description": "근거 문서로 뒷받침되는 수동 조치를 쓸 수 있으면 true"},
        "summary": {"type": "string", "description": "무엇을 왜 바꾸는 초안인지 1~2문장"},
        "covered_by_runbook": {"type": "string",
                               "description": "승인된 runbook으로 처리할 수 있는 조치가 있으면 그 runbook id(초안에는 넣지 않음)"},
        "steps": {"type": "array", "maxItems": 6, "items": {
            **_ITEM, "properties": {**_ITEM["properties"], "phase": {
                "type": "string", "enum": ["precheck", "change"],
                "description": "precheck = 변경 전 읽기 전용 확인, change = 변경"}},
            "required": ["phase", *_ITEM["required"]]}},
        "verification": {"type": "array", "maxItems": 3, "items": _ITEM,
                         "description": "검증 방법: 성공을 확인하는 읽기 전용 단계"},
        "rollback": {"type": "array", "maxItems": 3, "items": _ITEM, "description": "되돌리기"},
        "not_applicable_reason": {"type": "string"},
    },
    "required": ["applicable", "summary", "steps", "verification", "rollback"],
}

FIX_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "S1, V1, R1 ..."},
            "verdict": {"type": "string", "enum": ["grounded", "ungrounded"]},
            "destructive": {"type": "boolean", "description": "삭제·종료·보안그룹·IAM·중지/재부팅 등 되돌리기 어려운 변경"},
            "note": {"type": "string", "description": "판단 근거 한 줄(어느 문서 발췌·근거와 맞는지/다른지)"}},
            "required": ["ref", "verdict", "destructive", "note"]}},
        "reviewer_note": {"type": "string", "description": "사람이 실행 전에 읽을 한 문장 총평"},
    },
    "required": ["items", "reviewer_note"],
}

# --------------------------------------------------------------------------- patterns
FORBIDDEN = re.compile(r"0\.0\.0\.0/0|::/0", re.I)
DESTRUCTIVE = re.compile(
    r"\b(delete|terminate|remove|revoke|deregister|detach|disable|drop|destroy|purge|(?<!pending-)reboot|stop)[-\s]"
    r"|\bauthorize-security-group|\bmodify-security-group|security[\s-]?group|보안\s*그룹|\biam\b|"
    r"\b(attach|put)-(role|user|group)-policy|삭제|종료|중지|재부팅|정지",
    re.I)
SECRET_PATTERNS = [
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "<access-key-id>"),
    (re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}"), "<slack-token>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "<private-key>"),
    (re.compile(r"(?i)((?:password|passwd|secret|token|master-user-password)[=\s:]+)(?![<$])([^\s'\"]{4,})"),
     r"\1<secret>"),
]
RESOURCE_IDS = [
    (re.compile(r"\bi-[0-9a-f]{8,17}\b"), "<instance-id>"),
    (re.compile(r"\bsg-[0-9a-f]{8,17}\b"), "<security-group-id>"),
    (re.compile(r"\bsubnet-[0-9a-f]{8,17}\b"), "<subnet-id>"),
    (re.compile(r"\bvpc-[0-9a-f]{8,17}\b"), "<vpc-id>"),
    (re.compile(r"\bvpce-[0-9a-f]{8,17}\b"), "<vpc-endpoint-id>"),
    (re.compile(r"\bd-[A-Z0-9]{9}\b"), "<deployment-id>"),
    (re.compile(r"\barn:aws[a-z-]*:[^\s'\"]+"), "<arn>"),
    (re.compile(r"(?<![\d.])\d{12}(?![\d.])"), "<account-id>"),
]
READ_VERBS = re.compile(r"^(describe|list|get|lookup|filter|search|head|show|view|batch-get|query|scan|"
                        r"select|test-|validate|estimate|simulate|preview)", re.I)
_SQL_READ = re.compile(r"^\s*(show|select|explain)\b", re.I)
_SQL_WRITE = re.compile(r"\b(insert|update|delete|alter|drop|create|grant|revoke|truncate|set|kill|vacuum)\b", re.I)
_SQL_IN_CLI = re.compile(r"""(?:^|\s)-(?:c|e)\s+["'](.+?)["']""")
_AWS_CMD = re.compile(r"\baws\s+(?:--[\w-]+(?:[=\s]\S+)?\s+)*([a-z0-9-]+)\s+([a-z0-9-]+)", re.I)


def command_is_read_only(command: str) -> bool | None:
    """True/False for `aws <service> <operation>` commands and plain SQL reads (SHOW/SELECT/EXPLAIN, also inside
    `psql -c "..."` / `mysql -e "..."`); None if unknown (the gate then never claims it is read-only)."""
    m = _AWS_CMD.search(command or "")
    if not m:
        inner = _SQL_IN_CLI.search(command or "")
        sql = inner.group(1) if inner else (command or "")
        if _SQL_READ.match(sql):
            return not _SQL_WRITE.search(sql)
        return None
    service, op = m.group(1), m.group(2)
    if service == "logs" and op in {"start-query", "start-live-tail"}:
        return True
    if service == "s3" and op in {"ls"}:
        return True
    return bool(READ_VERBS.match(op))


def _redact(text: str, evidence_text: str, issues: list[str], ref_hint: str) -> str:
    out = text or ""
    for rx, repl in SECRET_PATTERNS:
        if rx.search(out):
            out = rx.sub(repl, out)
            issues.append(f"비밀값 형태 문자열을 자리표시자로 바꿈 ({ref_hint})")
    for rx, placeholder in RESOURCE_IDS:
        def sub(m: re.Match) -> str:
            return m.group(0) if m.group(0) in evidence_text else placeholder
        new = rx.sub(sub, out)
        if new != out:
            issues.append(f"근거에 없는 리소스 값 → {placeholder} ({ref_hint})")
            out = new
    return out


def _clean_item(raw: Any, section: str, doc_index: dict[str, str], evidence_text: str,
                issues: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    purpose = str(raw.get("purpose") or "").strip()[:200] or "(목적 없음)"
    hint = purpose[:40]
    urls_given = [str(u) for u in (raw.get("doc_urls") or []) if u]
    urls = list(dict.fromkeys(doc_index[norm_url(u)] for u in urls_given if norm_url(u) in doc_index))
    if len(urls) < len(urls_given):
        issues.append(f"도구 결과에 없는 URL 제거 ({hint})")
    if not urls:
        issues.append(f"근거 문서 URL 없음 → 삭제: {hint}")
        return None
    command = _redact(str(raw.get("command") or "").strip()[:600], evidence_text, issues, hint)
    console = _redact(str(raw.get("console_path") or "").strip()[:400], evidence_text, issues, hint)
    if not command and not console:
        issues.append(f"명령·콘솔 경로 없음 → 삭제: {hint}")
        return None
    if FORBIDDEN.search(command) or FORBIDDEN.search(console):
        issues.append(f"정책 금지(전체 개방) → 삭제: {hint}")
        return None
    derived = command_is_read_only(command) if command else None
    read_only = bool(raw.get("read_only")) if derived is None else derived
    if derived is not None and derived != bool(raw.get("read_only")):
        issues.append(f"읽기 전용 표시를 명령 동사 기준으로 고침 → {'읽기 전용' if derived else '변경'} ({hint})")
    if derived is None and command and raw.get("read_only"):
        read_only = False  # unknown non-AWS-CLI command: never claim it is safe
        issues.append(f"AWS CLI가 아닌 명령은 읽기 전용으로 표시하지 않음 ({hint})")
    phase = raw.get("phase") if section == "steps" else section
    if section == "steps":
        phase = "precheck" if phase == "precheck" else "change"
        if phase == "precheck" and not read_only:
            issues.append(f"사전 확인에 변경 명령 → 변경 단계로 옮김 ({hint})")
            phase = "change"
    if section == "verification" and not read_only:
        issues.append(f"검증 단계에 변경 명령 → 삭제: {hint}")
        return None
    destructive = (not read_only) and bool(DESTRUCTIVE.search(f"{command} {console} {purpose}"))
    return {"phase": phase, "purpose": purpose, "command": command, "console_path": console,
            "read_only": read_only, "risk_note": str(raw.get("risk_note") or "").strip()[:300],
            "doc_urls": urls, "destructive": destructive}


def gate_manual_fix(draft: dict | None, doc_hits: dict[str, dict], evidence_text: str = "") -> tuple[dict, list[str]]:
    """Code gate. `doc_hits` = URLs the doc tool returned in this run (RunContext.doc_hits)."""
    issues: list[str] = []
    draft = draft if isinstance(draft, dict) else {}
    doc_index = {norm_url(u): u for u in doc_hits}
    out: dict[str, Any] = {"summary": str(draft.get("summary") or "").strip()[:400],
                           "covered_by_runbook": str(draft.get("covered_by_runbook") or "").strip()[:80],
                           "not_applicable_reason": str(draft.get("not_applicable_reason") or "").strip()[:300]}
    for section, prefix in SECTIONS:
        items = [_clean_item(r, section, doc_index, evidence_text, issues) for r in (draft.get(section) or [])]
        items = [i for i in items if i]
        if section == "steps":
            items = [i for i in items if i["phase"] == "precheck"] + [i for i in items if i["phase"] == "change"]
        for n, item in enumerate(items, 1):
            item["ref"] = f"{prefix}{n}"
        out[section] = items
    out["applicable"] = bool(draft.get("applicable")) and any(s["phase"] == "change" for s in out["steps"])
    if draft.get("applicable") and not out["applicable"]:
        issues.append("근거 있는 변경 단계 없음: 확인 단계만 표시")
    out["doc_urls"] = _all_urls(out)
    return out, issues


def _all_urls(fix: dict) -> list[str]:
    return list(dict.fromkeys(u for section, _ in SECTIONS for i in fix.get(section, []) for u in i["doc_urls"]))


def apply_review(fix: dict, review: dict | None) -> tuple[dict, list[str]]:
    """Reviewer can only remove items or add destructive flags. No review = nothing is shown."""
    fix = json.loads(json.dumps(fix))
    issues: list[str] = []
    if not isinstance(review, dict):
        for section, _ in SECTIONS:
            fix[section] = []
        fix["applicable"] = False
        fix["doc_urls"] = []
        fix["reviewer_note"] = "검토자 실행 실패: 단계별 근거 대조를 못 해 초안을 표시하지 않음"
        return fix, ["검토자 실행 실패 → 초안 단계 표시 안 함"]
    verdicts = {str(i.get("ref", "")).strip().upper(): i for i in review.get("items", []) if isinstance(i, dict)}
    for section, _ in SECTIONS:
        kept = []
        for item in fix.get(section, []):
            v = verdicts.get(item["ref"])
            if v is None:
                issues.append(f"검토 누락 → 삭제: {item['ref']} {item['purpose'][:40]}")
                continue
            if v.get("verdict") != "grounded":
                issues.append(f"근거 없음 → 삭제: {item['ref']} {item['purpose'][:40]} ({str(v.get('note', ''))[:80]})")
                continue
            if v.get("destructive") and not item["read_only"]:
                item["destructive"] = True
            item["review_note"] = str(v.get("note") or "")[:200]
            kept.append(item)
        fix[section] = kept
    fix["applicable"] = fix.get("applicable", False) and any(s["phase"] == "change" for s in fix["steps"])
    fix["doc_urls"] = _all_urls(fix)
    fix["reviewer_note"] = str(review.get("reviewer_note") or "")[:300]
    return fix, issues


def review_payload(fix: dict) -> str:
    """The gated draft as the reviewer sees it (refs assigned by code)."""
    rows = []
    for section, _ in SECTIONS:
        for i in fix.get(section, []):
            rows.append({k: i[k] for k in ("ref", "phase", "purpose", "command", "console_path", "read_only",
                                           "risk_note", "doc_urls")})
    return json.dumps({"summary": fix.get("summary"), "items": rows}, ensure_ascii=False, indent=1)


def doc_snippets(fix: dict, doc_hits: dict[str, dict], per_doc: int = 2500, max_chars: int = 12000) -> str:
    parts, total = [], 0
    for url in fix.get("doc_urls", []):
        hit = doc_hits.get(url) or {}
        chunk = f"## {url}\n제목: {hit.get('title', '')}\n{(hit.get('text') or '')[:per_doc]}\n"
        if total + len(chunk) > max_chars:
            parts.append("(이후 문서 생략: 길이 제한)")
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts) or "(문서 없음)"
