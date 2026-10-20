"""Tool logic without the Agent SDK: the same code runs in-process (local mode) and inside the
AgentCore Gateway Lambda targets (deployed mode).

Every call returns a small JSON "envelope" so the caller (the in-process MCP tool wrapper in
tools/__init__.py) can register evidence, cap the size, audit and render the MCP result the same
way whether the work ran locally or behind the Gateway:

  {"ok": true,  "kind": "evidence", "tool": "...", "summary": "...", "data": {...}}   read tools
  {"ok": true,  "kind": "json",     "body": {...}}                                    runbook tools
  {"ok": false, "error": "...", "code": "..."}                                        actionable error

This module must not import claude_agent_sdk (the Lambda bundle does not ship it).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..errors import ApprovalError
from ..runbooks import RunbookRegistry
from .backends import Backend
from .summarize import cost_delta, date_range_for_cost, series_stats, summarize_logs, summarize_trail

if TYPE_CHECKING:
    from ..approvals import ApprovalService

READ_TOOLS = (
    "cloudwatch_alarm_status",
    "cloudwatch_metric_summary",
    "logs_error_summary",
    "cloudtrail_recent_changes",
    "cost_daily_delta",
    "evidence_get",
)
RUNBOOK_TOOLS = ("runbook_list", "runbook_get", "runbook_propose")
ALL_TOOLS = READ_TOOLS + RUNBOOK_TOOLS
# Tools whose work is local to the agent (its own thread notes), never sent to the Gateway.
LOCAL_ONLY_TOOLS = frozenset({"evidence_get"})
# Tools executed by the core (locally or behind the Gateway).
CORE_TOOLS = tuple(t for t in ALL_TOOLS if t not in LOCAL_ONLY_TOOLS)


@dataclass
class Caller:
    """Trusted request context. Filled by code (never by the model) and, in deployed mode, sent to
    the Gateway as the `caller` argument of runbook_propose (the wrapper overwrites whatever the
    model put there)."""

    thread_id: str
    user_id: str
    channel_id: str | None = None
    known_evidence_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"thread_id": self.thread_id, "user_id": self.user_id, "channel_id": self.channel_id or "",
                "known_evidence_ids": list(self.known_evidence_ids)}

    @classmethod
    def from_json(cls, d: dict | None) -> "Caller":
        d = d or {}
        return cls(thread_id=str(d.get("thread_id") or ""), user_id=str(d.get("user_id") or ""),
                   channel_id=(d.get("channel_id") or None),
                   known_evidence_ids=[str(x) for x in (d.get("known_evidence_ids") or [])])


def _bounded_int(args: dict, name: str, default: int, lo: int, hi: int) -> int:
    value = args.get(name, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _err(message: str, code: str = "bad_request") -> dict:
    return {"ok": False, "error": message, "code": code}


def _evidence(tool: str, summary: str, data: Any) -> dict:
    return {"ok": True, "kind": "evidence", "tool": tool, "summary": summary, "data": data}


def tool_specs(service_names: list[str], metric_hint: str) -> dict[str, tuple[str, dict]]:
    """name -> (description, JSON input schema). Shared by the in-process tools and the Gateway
    target tool schemas (infra/), so the model-facing contract is defined once."""
    return {
        "cloudwatch_alarm_status": (
            "CloudWatch 알람 현재 상태 요약. 서비스별로 ALARM 상태와 시작 시각, 지속 시간, 임계값을 돌려준다. "
            "증상 범위를 처음 파악할 때 쓴다.",
            {"type": "object", "properties": {
                "service": {"type": "string", "description": f"서비스 이름(생략 시 전체). 가능한 값: {service_names}"},
                "state": {"type": "string", "enum": ["ALARM", "OK", "INSUFFICIENT_DATA", "ANY"],
                          "description": "필터할 상태. 기본 ALARM"}},
             "required": []}),
        "cloudwatch_metric_summary": (
            "서비스 지표 1개의 요약(기준값 중앙값, 현재값, 최고값, 변화 시작 시점, 압축된 5분 시계열). "
            "원시 데이터 대신 요약만 돌려준다. 증상의 시작 시각과 대안 원인(트래픽 급증, CPU 포화 등)을 확인할 때 쓴다.",
            {"type": "object", "properties": {
                "service": {"type": "string", "description": f"서비스 이름. 가능한 값: {service_names}"},
                "metric": {"type": "string", "description": "지표 키. 서비스별 가능한 값: " + metric_hint},
                "minutes": {"type": "integer", "description": "조회 기간(분), 30~1440, 기본 120"}},
             "required": ["service", "metric"]}),
        "logs_error_summary": (
            "CloudWatch Logs Insights로 ERROR/WARN/timeout 로그를 집계한 요약: 인스턴스별 건수, 인스턴스별 앱 버전, "
            "급증 시작 구간, 상위 에러 시그니처와 가려진 샘플. 원문 로그는 돌려주지 않는다(행 상한 적용).",
            {"type": "object", "properties": {
                "service": {"type": "string", "description": f"서비스 이름. 가능한 값: {service_names}"},
                "minutes": {"type": "integer", "description": "조회 기간(분), 15~720, 기본 90"}},
             "required": ["service"]}),
        "cloudtrail_recent_changes": (
            "최근 쓰기(변경) API 이벤트 요약(CloudTrail LookupEvents, 읽기 이벤트 제외). 누가 언제 무엇을 바꿨는지와 "
            "대상 서비스 리소스와의 연관 표시. 증상 직전·직후 변경을 찾을 때 쓴다.",
            {"type": "object", "properties": {
                "hours": {"type": "integer", "description": "조회 기간(시간), 1~72, 기본 6"},
                "service": {"type": "string", "description": "연관 표시 기준 서비스(선택)"}},
             "required": []}),
        "cost_daily_delta": (
            "Cost Explorer 서비스별 일별 비용: 어제 vs 그제 증감, 7일 평균, 상위 변동 서비스. 당일(추정치)은 비교에서 제외.",
            {"type": "object", "properties": {
                "days": {"type": "integer", "description": "조회 일수 3~14, 기본 8"}},
             "required": []}),
        "evidence_get": (
            "이 스레드에서 이미 수집한 근거(EV-..)의 상세 내용을 다시 읽는다. 노트에는 한 줄 요약만 있으므로 "
            "세부 수치가 필요할 때만 쓴다.",
            {"type": "object", "properties": {"evidence_id": {"type": "string", "description": "예: EV-03"}},
             "required": ["evidence_id"]}),
        "runbook_list": (
            "변경 허용 목록(runbook) 한 줄 목록. status=approved만 제안할 수 있다. 본문은 runbook_get으로 필요할 때 읽는다.",
            {"type": "object", "properties": {}, "required": []}),
        "runbook_get": (
            "runbook 본문(언제 쓰는지, 사전 점검, 파라미터 규칙, 완료 조건)을 읽는다.",
            {"type": "object", "properties": {"runbook_id": {"type": "string"}}, "required": ["runbook_id"]}),
        "runbook_propose": (
            "허용 목록 runbook 실행을 '제안'한다. 실행하지 않는다. 승인 대기 기록만 만들고 approval_id를 돌려준다. "
            "사람이 Slack 승인 버튼을 눌러야 별도 실행기가 실행한다. 한 번에 변경 하나, 대상 하나. "
            "evidence_ids에는 파라미터를 정한 근거(EV-..)를 넣는다.",
            {"type": "object", "properties": {
                "runbook_id": {"type": "string", "description": "runbook_list의 id"},
                "params": {"type": "object", "description": "runbook 파라미터. 예: {\"service\": \"checkout-api\", "
                                                            "\"InstanceId\": \"i-...\"}"},
                "reason": {"type": "string", "description": "왜 이 변경인지 근거 요약(승인자가 읽는다)"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}, "description": "근거 ID 목록"}},
             "required": ["runbook_id", "params", "reason", "evidence_ids"]}),
    }


def metric_hint(services: dict) -> str:
    return "; ".join(f"{n}={sorted(s.metrics)}" for n, s in services.items() if s.metrics)


class ToolCore:
    """Executes the task-level tools against a Backend. No evidence storage, no MCP rendering."""

    def __init__(self, backend: Backend, runbooks: RunbookRegistry, approvals: "ApprovalService | None" = None,
                 display_tz: str = "Asia/Seoul", log_row_cap: int = 200):
        self.backend = backend
        self.runbooks = runbooks
        self.approvals = approvals
        self.tz = ZoneInfo(display_tz)
        self.display_tz = display_tz
        self.log_row_cap = log_row_cap

    def _fmt(self, t: datetime) -> str:
        now = self.backend.now().astimezone(self.tz)
        local = t.astimezone(self.tz)
        return local.strftime("%H:%M") if local.date() == now.date() else local.strftime("%m-%d %H:%M")

    def _service(self, name: str | None, *, required: bool) -> str | None:
        services = self.backend.services()
        if name is None or name == "":
            if required:
                raise ValueError(f"service가 필요합니다. 가능한 값: {sorted(services)}")
            return None
        if name not in services:
            raise ValueError(f"알 수 없는 service {name!r}. 가능한 값: {sorted(services)}")
        return name

    async def call(self, name: str, args: dict, caller: Caller) -> dict:
        fn = getattr(self, f"_t_{name}", None)
        if name not in CORE_TOOLS or fn is None:
            return _err(f"알 수 없는 도구 {name!r}", "unknown_tool")
        try:
            return await fn(args or {}, caller)
        except ValueError as e:
            return _err(str(e))

    # ---------------------------------------------------------------- read tools
    async def _t_cloudwatch_alarm_status(self, args: dict, caller: Caller) -> dict:
        service = self._service(args.get("service"), required=False)
        state = args.get("state") or "ALARM"
        services = self.backend.services()
        prefix = services[service].alarm_prefix if service else None
        alarms = await asyncio.to_thread(self.backend.describe_alarms, prefix, None if state == "ANY" else state)
        now = self.backend.now()
        items = []
        for a in sorted(alarms, key=lambda a: a["since"]):
            since = datetime.fromisoformat(a["since"].replace("Z", "+00:00"))
            items.append({"name": a["name"], "state": a["state"], "service": a["service"], "since": self._fmt(since),
                          "since_iso": a["since"], "for_minutes": int((now - since).total_seconds() // 60),
                          "metric": a["metric"], "threshold": a["threshold"], "unit": a["unit"],
                          "reason": a["reason"][:140]})
        firing = [i for i in items if i["state"] == "ALARM"]
        summary = (f"{state} 알람 {len(items)}건" + (": " + ", ".join(f"{i['name']}({i['since']}부터)" for i in firing[:4])
                                                   if firing else ""))
        return _evidence("cloudwatch_alarm_status", summary, {"alarms": items})

    async def _t_cloudwatch_metric_summary(self, args: dict, caller: Caller) -> dict:
        service = self._service(args.get("service"), required=True)
        services = self.backend.services()
        metric = args.get("metric", "")
        spec = services[service].metrics.get(metric)
        if spec is None:
            return _err(f"{service}에 {metric!r} 지표가 없습니다. 가능한 값: {sorted(services[service].metrics)}")
        minutes = _bounded_int(args, "minutes", 120, 30, 1440)
        end = self.backend.now()
        points = await asyncio.to_thread(self.backend.metric_series, service, metric, end - timedelta(minutes=minutes), end)
        stats = series_stats(points, spec.unit, self._fmt)
        stats["desc"] = spec.desc
        if stats.get("points") == 0:
            summary = f"{service} {metric}: 데이터 없음"
        else:
            summary = (f"{service} {metric}({spec.unit}): 기준 {stats['baseline_median']} → 현재 {stats['current']} "
                       f"({stats['current_vs_baseline']}), 변화 시작 {stats['change_point'] or '없음'}, "
                       f"최고 {stats['peak']['value']}@{stats['peak']['at']}")
        return _evidence("cloudwatch_metric_summary", summary, stats)

    async def _t_logs_error_summary(self, args: dict, caller: Caller) -> dict:
        service = self._service(args.get("service"), required=True)
        minutes = _bounded_int(args, "minutes", 90, 15, 720)
        end = self.backend.now()
        res = await asyncio.to_thread(self.backend.logs_rows, service, end - timedelta(minutes=minutes), end,
                                      self.log_row_cap)
        data = summarize_logs(res, self._fmt)
        if res.total == 0:
            summary = f"{service}: 최근 {minutes}분 필터 일치 로그 없음"
        else:
            top_inst, top_n = next(iter(data["by_instance"].items()))
            sig = data["signatures"][0]["signature"][:90] if data["signatures"] else "-"
            summary = (f"{service} 최근 {minutes}분 일치 {res.total}건, 최다 {top_inst} {top_n}건"
                       f"({top_n * 100 // max(res.total, 1)}%), 급증 시작 {data['first_spike_bin'] or '없음'}, 최다 시그니처: {sig}")
        return _evidence("logs_error_summary", summary, data)

    async def _t_cloudtrail_recent_changes(self, args: dict, caller: Caller) -> dict:
        service = self._service(args.get("service"), required=False)
        hours = _bounded_int(args, "hours", 6, 1, 72)
        end = self.backend.now()
        events = await asyncio.to_thread(self.backend.trail_events, end - timedelta(hours=hours), end, 50)
        terms: set[str] = set()
        if service:
            svc = self.backend.services()[service]
            terms |= {service, *svc.security_groups}
            terms |= {i["id"] for i in await asyncio.to_thread(self.backend.service_instances, service)}
        data = summarize_trail(events, terms, self._fmt)
        related = [e for e in data["events"] if e["related_to"]]
        summary = (f"최근 {hours}시간 쓰기 이벤트 {data['write_events_total']}건"
                   + (f", {service} 관련 {len(related)}건: " + " / ".join(f"{e['at']} {e['event']}({e['actor']})"
                                                                     for e in related[:3]) if service else ""))
        return _evidence("cloudtrail_recent_changes", summary, data)

    async def _t_cost_daily_delta(self, args: dict, caller: Caller) -> dict:
        days = _bounded_int(args, "days", 8, 3, 14)
        start, end = date_range_for_cost(self.backend.now(), days)
        rows = await asyncio.to_thread(self.backend.cost_by_service, start, end)
        data = cost_delta(rows, self.backend.now().date())
        if not data["ok"]:
            return _err(data["reason"], "no_data")
        top = data["top_movers"][0]
        pct = f"{top['delta_pct']:+.1f}%" if top["delta_pct"] is not None else "n/a"
        summary = (f"{data['compare']['yesterday']} vs {data['compare']['day_before']}: 총 {data['total']['delta']:+.2f} USD, "
                   f"최대 변동 {top['service']} {top['delta']:+.2f} USD ({pct})")
        return _evidence("cost_daily_delta", summary, data)

    # ---------------------------------------------------------------- runbooks
    async def _t_runbook_list(self, args: dict, caller: Caller) -> dict:
        items = [{"id": rb.id, "title": rb.title, "status": rb.status, "ssm_document": rb.ssm_document,
                  "risk": rb.risk, "params": sorted(rb.params), "summary": rb.summary}
                 for rb in self.runbooks.list()]
        return {"ok": True, "kind": "json",
                "body": {"runbooks": items, "note": "draft는 실행 불가. 맞는 것이 없으면 제안하지 않는다."}}

    async def _t_runbook_get(self, args: dict, caller: Caller) -> dict:
        rb = self.runbooks.get(args.get("runbook_id", ""))
        if rb is None:
            return _err(f"없는 runbook. 가능한 id: {[r.id for r in self.runbooks.list()]}", "unknown_runbook")
        return {"ok": True, "kind": "json", "body": {"id": rb.id, "status": rb.status, "ssm_document": rb.ssm_document,
                                                     "params": rb.params, "body": rb.body}}

    async def _t_runbook_propose(self, args: dict, caller: Caller) -> dict:
        if self.approvals is None:
            return _err("이 실행 환경에는 승인 저장소가 없습니다.", "not_configured")
        if not caller.thread_id or not caller.user_id:
            return _err("호출 컨텍스트(thread/user)가 없습니다.", "no_caller")
        try:
            rec = await asyncio.to_thread(
                self.approvals.propose, thread_id=caller.thread_id, channel_id=caller.channel_id,
                requested_by=caller.user_id, runbook_id=args.get("runbook_id", ""), params=args.get("params") or {},
                reason=args.get("reason", ""), evidence_ids=list(args.get("evidence_ids") or []),
                known_evidence=set(caller.known_evidence_ids) if caller.known_evidence_ids else None)
        except ApprovalError as e:
            return _err(f"[{e.code}] {e.message}", e.code)
        return {"ok": True, "kind": "json", "approval_id": rec.id, "record": rec.to_dict(), "body": {
            "approval_id": rec.id, "status": "PENDING_APPROVAL", "document": rec.document, "params": rec.params,
            "expires_at": rec.expires_at,
            "note": "아직 아무것도 실행되지 않았다. 승인자가 Slack 버튼으로 승인해야 실행기가 실행하고 검증한다.",
        }}
