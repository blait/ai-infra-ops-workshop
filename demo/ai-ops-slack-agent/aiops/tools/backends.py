"""Data backends behind the agent tools.

MockBackend (AIOPS_MODE=mock) replays fixtures/checkout-incident/*.json and a persisted mock
clock, so rehearsals, tests and evals run offline and produce the same incident every time.
AwsBackend (AIOPS_MODE=aws) makes read-only boto3 calls only; the IAM policy for it is
iam/agent-readonly-policy.json. Neither backend can change anything.

Both return the same normalized shapes so the summarizers in summarize.py are shared.
"""

from __future__ import annotations

import json
import re
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

UTC = timezone.utc


def parse_ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class MetricSpec:
    key: str
    unit: str
    desc: str
    period: int = 300
    expression: str | None = None
    queries: tuple[dict, ...] = ()  # aws mode: GetMetricData MetricStat definitions


@dataclass(frozen=True)
class ServiceInfo:
    name: str
    description: str
    log_group: str | None
    alarm_prefix: str
    security_groups: tuple[str, ...] = ()
    metrics: dict[str, MetricSpec] = field(default_factory=dict)
    instance_tag: tuple[str, str] | None = None
    static_instances: tuple[dict, ...] = ()


@dataclass
class LogsResult:
    rows: list[dict]  # most recent first is NOT guaranteed; each {ts, stream, message}
    total: int
    count_by_stream: dict[str, int]
    count_by_bin: dict[str, int]
    query: str
    row_cap: int


class Backend(Protocol):
    def now(self) -> datetime: ...
    def services(self) -> dict[str, ServiceInfo]: ...
    def describe_alarms(self, prefix: str | None = None, state: str | None = None) -> list[dict]: ...
    def metric_series(self, service: str, metric: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]: ...
    def logs_rows(self, service: str, start: datetime, end: datetime, row_cap: int) -> LogsResult: ...
    def trail_events(self, start: datetime, end: datetime, max_events: int = 50) -> list[dict]: ...
    def cost_by_service(self, start: date, end: date) -> list[dict]: ...
    def service_instances(self, service: str) -> list[dict]: ...


LOG_FILTER = "filter @message like /(?i)(error|exception|timed out|timeout|warn|started)/"


def stratified_sample(rows: list[dict], row_cap: int) -> list[dict]:
    """Earliest half + latest half of the window, so first-seen times stay meaningful."""
    ordered = sorted(rows, key=lambda r: r["ts"])
    if len(ordered) <= row_cap:
        return ordered
    half = row_cap // 2
    return ordered[:half] + ordered[-(row_cap - half):]


# --------------------------------------------------------------------------- mock state
class MockState:
    """Persisted mock world state (mock clock + restarts + executions) shared across processes."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict:
        if not self.path.exists():
            return {"advance_minutes": 0, "restarts": [], "executions": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def update(self, fn) -> Any:
        with self._lock:
            state = self.load()
            result = fn(state)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            return result

    def reset(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()


def parse_instance_map(raw: str | None) -> dict[str, str]:
    """AIOPS_MOCK_INSTANCE_MAP="i-fixture=i-real,..." -> {fixture: real}. Ids must look like EC2 ids."""
    out: dict[str, str] = {}
    for pair in (raw or "").split(","):
        if not pair.strip():
            continue
        src, sep, dst = pair.partition("=")
        src, dst = src.strip(), dst.strip()
        if not sep or not re.fullmatch(r"i-[0-9a-f]{8,17}", src) or not re.fullmatch(r"i-[0-9a-f]{8,17}", dst):
            raise ValueError(f"bad AIOPS_MOCK_INSTANCE_MAP entry {pair!r} (expected i-xxx=i-yyy)")
        out[src] = dst
    return out


class MockBackend:
    """Fixture world. `instance_map` swaps fixture instance ids for real ones (deployed demo: the
    leaking instance in the story becomes the one tagged t4g.nano, so an approved restart runs a
    real SSM Automation on a real instance while the metrics/logs stay the rehearsed fixtures)."""

    is_mock = True

    def __init__(self, fixtures_dir: Path, state: "MockState", instance_map: dict[str, str] | None = None):
        self.dir = fixtures_dir
        self.state = state
        self.instance_map = dict(instance_map or {})

        def load(name: str):
            text = (fixtures_dir / name).read_text(encoding="utf-8")
            for src, dst in self.instance_map.items():
                text = text.replace(src, dst)
            return json.loads(text)

        self._inventory = load("inventory.json")
        self._alarms = load("alarms.json")
        self._metrics = load("metrics.json")
        self._post_restart = load("post_restart.json")
        self._logs = load("logs.json")
        self._trail = load("cloudtrail.json")
        self._cost = load("cost.json")
        self._base_now = parse_ts(self._inventory["now"])

    # -- clock / restart overlay
    def now(self) -> datetime:
        return self._base_now + timedelta(minutes=self.state.load().get("advance_minutes", 0))

    def _restart_time(self) -> datetime | None:
        target = self._post_restart["instance"]
        for r in self.state.load().get("restarts", []):
            if r["instance"] == target:
                return parse_ts(r["at"])
        return None

    def services(self) -> dict[str, ServiceInfo]:
        out = {}
        for name, s in self._inventory["services"].items():
            metrics = {k: MetricSpec(key=k, unit=m["unit"], desc=m["desc"]) for k, m in s.get("metrics", {}).items()}
            out[name] = ServiceInfo(
                name=name,
                description=s["description"],
                log_group=s.get("log_group"),
                alarm_prefix=s.get("alarm_prefix", f"{name}-"),
                security_groups=tuple(s.get("security_groups", [])),
                metrics=metrics,
                static_instances=tuple(s.get("instances", [])),
            )
        return out

    def describe_alarms(self, prefix: str | None = None, state: str | None = None) -> list[dict]:
        restarted_at = self._restart_time()
        out = []
        for a in self._alarms:
            rec = {
                "name": a["AlarmName"], "state": a["StateValue"], "since": a["StateUpdatedTimestamp"],
                "metric": a["MetricKey"], "threshold": a["Threshold"], "unit": a["Unit"],
                "reason": a["StateReason"], "service": a["Service"],
            }
            if restarted_at and a["AlarmName"] in self._post_restart["alarms_to_ok"]:
                rec.update(state="OK", since=(restarted_at + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           reason="Threshold Crossed: datapoints were not greater than the threshold.")
            if prefix and not rec["name"].startswith(prefix):
                continue
            if state and rec["state"] != state:
                continue
            out.append(rec)
        return out

    def metric_series(self, service: str, metric: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        try:
            raw = self._metrics[service][metric]
        except KeyError:
            raise KeyError(f"no metric {metric!r} for service {service!r}") from None
        points = [(parse_ts(t), float(v)) for t, v in raw]
        restarted_at = self._restart_time()
        overlay = self._post_restart.get(service, {}).get(metric)
        if restarted_at and overlay:
            points = [p for p in points if p[0] <= restarted_at]
            points += [(restarted_at + timedelta(minutes=m), float(v)) for m, v in overlay]
        now = self.now()
        return [p for p in points if start <= p[0] <= min(end, now)]

    def logs_rows(self, service: str, start: datetime, end: datetime, row_cap: int) -> LogsResult:
        svc = self.services().get(service)
        if svc is None:
            raise KeyError(f"unknown service {service!r}")
        if service != "checkout-api":  # fixtures only carry checkout-api application logs
            return LogsResult([], 0, {}, {}, LOG_FILTER, row_cap)
        rows = [r for r in self._logs if start <= parse_ts(r["ts"]) <= min(end, self.now())]
        by_stream: dict[str, int] = {}
        by_bin: dict[str, int] = {}
        for r in rows:
            by_stream[r["stream"]] = by_stream.get(r["stream"], 0) + 1
            t = parse_ts(r["ts"])
            b = t.replace(minute=t.minute - t.minute % 5, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")
            by_bin[b] = by_bin.get(b, 0) + 1
        return LogsResult(stratified_sample(rows, row_cap), len(rows), by_stream, dict(sorted(by_bin.items())),
                          LOG_FILTER, row_cap)

    def trail_events(self, start: datetime, end: datetime, max_events: int = 50) -> list[dict]:
        out = []
        for e in self._trail:
            t = parse_ts(e["EventTime"])
            if start <= t <= min(end, self.now()):
                out.append({
                    "time": e["EventTime"], "event": e["EventName"], "source": e["EventSource"],
                    "actor": e["Username"], "read_only": e["ReadOnly"] == "true",
                    "resources": [r["ResourceName"] for r in e.get("Resources", [])],
                    "params": e.get("RequestParameters") or {},
                })
        return sorted(out, key=lambda e: e["time"], reverse=True)[:max_events]

    def cost_by_service(self, start: date, end: date) -> list[dict]:
        return [r for r in self._cost if start.isoformat() <= r["date"] < end.isoformat()]

    def service_instances(self, service: str) -> list[dict]:
        svc = self.services().get(service)
        return [dict(i) for i in svc.static_instances] if svc else []

    def record_restart(self, instance_id: str) -> None:
        """A real executor finished a restart: move the mock world like MockExecutor would."""
        at = self.now().strftime("%Y-%m-%dT%H:%M:%SZ")

        def fn(s):
            s.setdefault("restarts", []).append({"instance": instance_id, "at": at})
            if instance_id == self._post_restart.get("instance"):
                s["advance_minutes"] = s.get("advance_minutes", 0) + int(self._post_restart.get("advance_minutes", 0))

        self.state.update(fn)


# --------------------------------------------------------------------------- aws (read-only)
def load_services_toml(path: Path) -> dict[str, ServiceInfo]:
    """Parse config/services.toml (see config/services.example.toml)."""
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: copy config/services.example.toml and fill in your resources")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = {}
    for name, s in data.get("services", {}).items():
        metrics = {}
        for key, m in s.get("metrics", {}).items():
            metrics[key] = MetricSpec(key=key, unit=m.get("unit", ""), desc=m.get("desc", key),
                                      period=int(m.get("period", 300)), expression=m.get("expression"),
                                      queries=tuple(m.get("queries", [])))
        tag = s.get("instance_tag")
        out[name] = ServiceInfo(
            name=name, description=s.get("description", ""), log_group=s.get("log_group"),
            alarm_prefix=s.get("alarm_prefix", f"{name}-"), security_groups=tuple(s.get("security_groups", [])),
            metrics=metrics, instance_tag=(tag["key"], tag["value"]) if tag else None,
        )
    return out


def _insights_ts(value: str) -> str:
    """Logs Insights returns '2026-10-15 04:42:10.123' (UTC); normalize to ISO-8601 Z."""
    if not value:
        return value
    try:
        return parse_ts(value.replace(" ", "T") + ("" if value.endswith("Z") else "Z")).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return value


class AwsBackend:
    """Read-only boto3 calls. Every API used here is listed in iam/agent-readonly-policy.json."""

    def __init__(self, services: dict[str, ServiceInfo], region: str, session=None, ce_region: str = "us-east-1"):
        import boto3
        from botocore.config import Config

        self._services = services
        session = session or boto3.Session(region_name=region)
        cfg = Config(retries={"mode": "standard", "max_attempts": 4}, user_agent_extra="aiops-slack-agent/0.1")
        self.cw = session.client("cloudwatch", region_name=region, config=cfg)
        self.logs = session.client("logs", region_name=region, config=cfg)
        self.trail = session.client("cloudtrail", region_name=region, config=cfg)
        self.ec2 = session.client("ec2", region_name=region, config=cfg)
        self.ce = session.client("ce", region_name=ce_region, config=cfg)  # Cost Explorer endpoint is us-east-1
        self.query_timeout_s = 25.0

    def now(self) -> datetime:
        return datetime.now(UTC)

    def services(self) -> dict[str, ServiceInfo]:
        return self._services

    def _service_for_alarm(self, name: str) -> str:
        for svc in self._services.values():
            if name.startswith(svc.alarm_prefix):
                return svc.name
        return "unknown"

    def describe_alarms(self, prefix: str | None = None, state: str | None = None) -> list[dict]:
        kwargs: dict[str, Any] = {"AlarmTypes": ["MetricAlarm", "CompositeAlarm"]}
        if prefix:
            kwargs["AlarmNamePrefix"] = prefix
        if state:
            kwargs["StateValue"] = state
        out = []
        for page in self.cw.get_paginator("describe_alarms").paginate(**kwargs):
            for a in page.get("MetricAlarms", []) + page.get("CompositeAlarms", []):
                out.append({
                    "name": a["AlarmName"], "state": a["StateValue"],
                    "since": a["StateUpdatedTimestamp"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "metric": a.get("MetricName") or "composite", "threshold": a.get("Threshold"),
                    "unit": a.get("Unit") or "", "reason": a.get("StateReason", "")[:300],
                    "service": self._service_for_alarm(a["AlarmName"]),
                })
        return out

    def metric_series(self, service: str, metric: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        spec = self._services[service].metrics[metric]
        queries = []
        for q in spec.queries:
            queries.append({
                "Id": q["id"],
                "MetricStat": {
                    "Metric": {"Namespace": q["namespace"], "MetricName": q["name"],
                               "Dimensions": [{"Name": k, "Value": v} for k, v in q.get("dimensions", {}).items()]},
                    "Period": spec.period,
                    "Stat": q.get("stat", "Average"),
                },
                "ReturnData": spec.expression is None,
            })
        if spec.expression:
            queries.append({"Id": "e1", "Expression": spec.expression, "Label": metric, "ReturnData": True})
        resp = self.cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end, ScanBy="TimestampAscending")
        result = next((r for r in resp["MetricDataResults"] if r.get("Timestamps")), None)
        if result is None:
            return []
        return [(t.astimezone(UTC), float(v)) for t, v in zip(result["Timestamps"], result["Values"])]

    def _run_insights(self, log_group: str, start: datetime, end: datetime, queries: dict[str, tuple[str, int]]
                      ) -> dict[str, list[dict]]:
        """Start all queries, then poll them together. Returns rows per query key."""
        qids = {key: self.logs.start_query(logGroupName=log_group, startTime=int(start.timestamp()),
                                           endTime=int(end.timestamp()), queryString=q, limit=limit)["queryId"]
                for key, (q, limit) in queries.items()}
        deadline = time.monotonic() + self.query_timeout_s
        results: dict[str, list[dict]] = {}
        while len(results) < len(qids):
            for key, qid in qids.items():
                if key in results:
                    continue
                resp = self.logs.get_query_results(queryId=qid)
                if resp["status"] == "Complete":
                    results[key] = [{f["field"]: f["value"] for f in row} for row in resp["results"]]
                elif resp["status"] in {"Failed", "Cancelled", "Timeout", "Unknown"}:
                    raise RuntimeError(f"Logs Insights query {key} {resp['status']}")
            if len(results) < len(qids):
                if time.monotonic() > deadline:
                    for key, qid in qids.items():
                        if key not in results:
                            self.logs.stop_query(queryId=qid)
                    raise TimeoutError(f"Logs Insights queries timed out after {self.query_timeout_s}s")
                time.sleep(0.8)
        return results

    def logs_rows(self, service: str, start: datetime, end: datetime, row_cap: int) -> LogsResult:
        group = self._services[service].log_group
        if not group:
            return LogsResult([], 0, {}, {}, LOG_FILTER, row_cap)
        half = max(1, row_cap // 2)
        base = f"fields @timestamp, @logStream, @message | {LOG_FILTER}"
        out = self._run_insights(group, start, end, {
            "earliest": (f"{base} | sort @timestamp asc | limit {half}", half),
            "latest": (f"{base} | sort @timestamp desc | limit {row_cap - half}", row_cap - half),
            "streams": (f"{LOG_FILTER} | stats count(*) as n by @logStream | sort n desc | limit 20", 20),
            "bins": (f"{LOG_FILTER} | stats count(*) as n by bin(5m) as t | sort t asc", 1000),
        })
        seen, rows = set(), []
        for r in out["earliest"] + out["latest"]:
            key = (r.get("@timestamp"), r.get("@logStream"), r.get("@message"))
            if key not in seen:
                seen.add(key)
                rows.append({"ts": _insights_ts(r.get("@timestamp", "")), "stream": r.get("@logStream", ""),
                             "message": r.get("@message", "")})
        by_stream = {r["@logStream"]: int(float(r["n"])) for r in out["streams"] if "@logStream" in r}
        by_bin = {_insights_ts(r["t"]): int(float(r["n"])) for r in out["bins"] if "t" in r}
        return LogsResult(rows, sum(by_stream.values()), by_stream, by_bin, base, row_cap)

    def trail_events(self, start: datetime, end: datetime, max_events: int = 50) -> list[dict]:
        out = []
        paginator = self.trail.get_paginator("lookup_events")
        pages = paginator.paginate(
            LookupAttributes=[{"AttributeKey": "ReadOnly", "AttributeValue": "false"}],
            StartTime=start, EndTime=end, PaginationConfig={"MaxItems": max_events, "PageSize": 50},
        )
        for page in pages:
            for e in page.get("Events", []):
                detail = json.loads(e.get("CloudTrailEvent") or "{}")
                ident = detail.get("userIdentity", {})
                actor = e.get("Username") or ident.get("arn", "").split(":")[-1]
                out.append({
                    "time": e["EventTime"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "event": e["EventName"], "source": e.get("EventSource", ""), "actor": actor,
                    "read_only": str(e.get("ReadOnly", "false")).lower() == "true",
                    "resources": [r.get("ResourceName", "") for r in e.get("Resources", [])],
                    "params": detail.get("requestParameters") or {},
                })
        return out

    def cost_by_service(self, start: date, end: date) -> list[dict]:
        rows, token = [], None
        while True:
            kwargs: dict[str, Any] = dict(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()}, Granularity="DAILY",
                Metrics=["UnblendedCost"], GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
            if token:
                kwargs["NextPageToken"] = token
            resp = self.ce.get_cost_and_usage(**kwargs)
            for day in resp["ResultsByTime"]:
                for g in day.get("Groups", []):
                    rows.append({"date": day["TimePeriod"]["Start"], "service": g["Keys"][0],
                                 "amount": round(float(g["Metrics"]["UnblendedCost"]["Amount"]), 2),
                                 "estimated": bool(day.get("Estimated", False))})
            token = resp.get("NextPageToken")
            if not token:
                return rows

    def service_instances(self, service: str) -> list[dict]:
        svc = self._services.get(service)
        if not svc or not svc.instance_tag:
            return []
        key, value = svc.instance_tag
        out = []
        pages = self.ec2.get_paginator("describe_instances").paginate(Filters=[
            {"Name": f"tag:{key}", "Values": [value]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ])
        for page in pages:
            for res in page.get("Reservations", []):
                for inst in res.get("Instances", []):
                    out.append({"id": inst["InstanceId"], "state": inst["State"]["Name"],
                                "az": inst.get("Placement", {}).get("AvailabilityZone", "")})
        return out


def build_backend(settings, state: "MockState | None" = None) -> Backend:
    if settings.mode == "mock":
        return MockBackend(settings.fixtures_dir, state or MockState(settings.mock_state_file),
                           instance_map=settings.mock_instance_map)
    return AwsBackend(load_services_toml(settings.services_file), region=settings.target_region)
