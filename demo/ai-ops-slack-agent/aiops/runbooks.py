"""Runbook registry = the change allowlist.

Each runbook is a Markdown file in runbooks/ with a small front-matter block. Only runbooks with
`status: approved` are executable; drafts are listed for context but every layer rejects them
(propose tool, approval service, executor). Adding a runbook is a code-reviewed change to this
folder, not something the model or a chat message can do.

BP (Agent Skills / just-in-time retrieval): the model first sees only the one-line list
(`runbook_list`), and loads a runbook body (`runbook_get`) only when it needs it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class VerifySpec:
    metric: str
    max_value: float
    window_minutes: int
    alarm_suffixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Runbook:
    id: str
    title: str
    status: str  # approved | draft
    ssm_document: str
    ssm_document_version: str
    risk: str
    summary: str
    params: dict[str, str]  # param name -> full-match regex
    ssm_params: tuple[str, ...]  # params forwarded to SSM (as single-item string lists)
    verify: VerifySpec | None
    body: str
    path: Path = field(compare=False)

    @property
    def executable(self) -> bool:
        return self.status == "approved"


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError("runbook must start with a '---' front-matter block")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("unterminated front-matter block")
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"bad front-matter line: {line!r}")
        meta[key.strip()] = value.strip()
    return meta, text[end + 5:].strip()


def load_runbook(path: Path) -> Runbook:
    meta, body = _parse_front_matter(path.read_text(encoding="utf-8"))
    params = {k.split(".", 1)[1]: v for k, v in meta.items() if k.startswith("param.")}
    for regex in params.values():
        re.compile(regex)
    verify = None
    if meta.get("verify_metric"):
        verify = VerifySpec(
            metric=meta["verify_metric"],
            max_value=float(meta["verify_max"]),
            window_minutes=int(meta.get("verify_window_minutes", "10")),
            alarm_suffixes=tuple(s.strip() for s in meta.get("verify_alarm_suffixes", "").split(",") if s.strip()),
        )
    status = meta.get("status", "draft")
    if status not in {"approved", "draft"}:
        raise ValueError(f"{path.name}: status must be approved|draft")
    return Runbook(
        id=meta["id"],
        title=meta["title"],
        status=status,
        ssm_document=meta["ssm_document"],
        ssm_document_version=meta.get("ssm_document_version", "$DEFAULT"),
        risk=meta.get("risk", "high"),
        summary=meta.get("summary", ""),
        params=params,
        ssm_params=tuple(s.strip() for s in meta.get("ssm_params", "").split(",") if s.strip()),
        verify=verify,
        body=body,
        path=path,
    )


class RunbookRegistry:
    def __init__(self, directory: Path):
        self.directory = directory
        self._by_id: dict[str, Runbook] = {}
        for path in sorted(directory.glob("*.md")):
            if path.name.lower() == "readme.md":
                continue
            rb = load_runbook(path)
            if rb.id in self._by_id:
                raise ValueError(f"duplicate runbook id {rb.id}")
            self._by_id[rb.id] = rb

    def list(self) -> list[Runbook]:
        return list(self._by_id.values())

    def get(self, runbook_id: str) -> Runbook | None:
        return self._by_id.get(runbook_id)

    @property
    def allowed_documents(self) -> frozenset[str]:
        return frozenset(rb.ssm_document for rb in self._by_id.values() if rb.executable)

    def validate_params(self, rb: Runbook, params: dict) -> tuple[dict[str, str], list[str]]:
        """Return (normalized_params, errors). Unknown or missing params are errors."""
        errors: list[str] = []
        if not isinstance(params, dict):
            return {}, ["params must be an object"]
        unknown = set(params) - set(rb.params)
        if unknown:
            errors.append(f"허용되지 않은 파라미터: {sorted(unknown)}")
        normalized: dict[str, str] = {}
        for name, regex in rb.params.items():
            value = params.get(name)
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            if not isinstance(value, str) or not value:
                errors.append(f"{name}: 문자열 1개가 필요합니다 (여러 대상을 한 번에 바꾸지 않음)")
                continue
            if not re.fullmatch(regex, value):
                errors.append(f"{name}: 형식 불일치 ({regex})")
                continue
            normalized[name] = value
        return normalized, errors

    @staticmethod
    def ssm_parameters(rb: Runbook, params: dict[str, str], assume_role_arn: str | None) -> dict[str, list[str]]:
        out = {name: [params[name]] for name in rb.ssm_params}
        if assume_role_arn:
            out["AutomationAssumeRole"] = [assume_role_arn]
        return out
