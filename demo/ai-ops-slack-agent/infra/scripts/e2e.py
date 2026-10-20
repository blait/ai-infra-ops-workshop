#!/usr/bin/env python3
"""End-to-end checks against the deployed AiopsAgentStack (operator credentials, us-east-1).

  python infra/scripts/e2e.py outputs                         # stack outputs (names only, ids redacted)
  python infra/scripts/e2e.py status                          # deployed AgentCore wiring/readiness (read-only)
  python infra/scripts/e2e.py invoke  "<prompt>" --session S  # AgentCore Runtime directly (streams progress)
  python infra/scripts/e2e.py notes --session S                # read this session from AgentCore Memory
  python infra/scripts/e2e.py gateway                         # direct Gateway calls: list / allowed / forbidden
  python infra/scripts/e2e.py slack-event "<text>" --channel C --thread-ts T [--user U]
  python infra/scripts/e2e.py slack-click <approval_id> --user U [--action approve|reject]
  python infra/scripts/e2e.py approval <approval_id>          # DynamoDB record (status, verify)
  python infra/scripts/e2e.py reset                           # mock world back to the incident (rehearsal)

The Slack signing secret is read from Secrets Manager inside this process and never printed.
Run with the demo venv: .venv/bin/python infra/scripts/e2e.py ...
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from aiops.cloud.slack import compute_signature  # noqa: E402
from aiops.docs import DocsError, GatewayDocsClient, parse_search  # noqa: E402
from aiops.gateway_client import GatewayToolClient, parse_targets  # noqa: E402

REGION = "us-east-1"
STACK = "AiopsAgentStack"


def outputs() -> dict:
    cf = boto3.client("cloudformation", region_name=REGION)
    out = cf.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    return {o["OutputKey"]: o["OutputValue"] for o in out}


def redact(s: str) -> str:
    import re

    return re.sub(r"\b\d{12}\b", "<acct>", s)


def cmd_outputs(_):
    for k, v in sorted(outputs().items()):
        print(f"{k:24} {redact(v)}")


def deployment_status() -> dict:
    """Inspect the deployed stack, never instantiate the local mock pipeline."""
    o = outputs()
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    runtime = control.get_agent_runtime(agentRuntimeId=o["RuntimeId"])
    gateway = control.get_gateway(gatewayIdentifier=o["GatewayId"])
    memory = control.get_memory(memoryId=o["MemoryId"])["memory"]
    policy = control.get_policy_engine(policyEngineId=o["PolicyEngineId"])
    flow = boto3.client("stepfunctions", region_name=REGION).describe_state_machine(
        stateMachineArn=o["StateMachineArn"])
    worker = boto3.client("lambda", region_name=REGION).get_function_configuration(
        FunctionName="aiops-slackworker")
    env = runtime.get("environmentVariables", {})
    worker_env = worker.get("Environment", {}).get("Variables", {})
    policy_config = gateway.get("policyEngineConfiguration", {})
    checks = {
        "runtime_ready": runtime.get("status") == "READY",
        "gateway_ready": gateway.get("status") == "READY",
        "gateway_iam_auth": gateway.get("authorizerType") == "AWS_IAM",
        "gateway_policy_enforce": policy_config.get("mode") == "ENFORCE",
        "gateway_policy_attached": o["PolicyEngineId"] in json.dumps(policy_config),
        "policy_active": policy.get("status") == "ACTIVE",
        "memory_active": memory.get("status") == "ACTIVE",
        "runtime_uses_gateway": env.get("AIOPS_GATEWAY_URL") == o["GatewayUrl"],
        "runtime_uses_memory": env.get("AIOPS_MEMORY_ID") == o["MemoryId"],
        "approval_flow_active": flow.get("status") == "ACTIVE",
        "approval_waits_for_callback": "waitForTaskToken" in flow.get("definition", ""),
        "slack_worker_uses_runtime": worker_env.get("AIOPS_RUNTIME_ARN") == o["RuntimeArn"],
    }
    return {
        "stack": STACK, "region": REGION, "core_ready": all(checks.values()),
        "runtime_version": runtime.get("agentRuntimeVersion"),
        "model": env.get("AIOPS_MODEL"), "data_source": env.get("AIOPS_MODE"),
        "memory_retention_days": memory.get("eventExpiryDuration"),
        "slack_dry_run": worker_env.get("AIOPS_SLACK_DRY_RUN", "true").lower() in {"1", "true", "yes", "on"},
        "checks": checks,
    }


def cmd_status(_):
    report = deployment_status()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["core_ready"]:
        raise SystemExit(2)


def cmd_notes(a):
    from aiops.memory import AgentCoreMemory, NotesMemorySync
    from aiops.notes import NotesStore

    # A temporary local view of cloud records; no writes to Memory or the local demo's var/.
    with tempfile.TemporaryDirectory(prefix="aiops-memory-view-") as directory:
        store = NotesStore(Path(directory))
        sync = NotesMemorySync(AgentCoreMemory(outputs()["MemoryId"], REGION), store)
        restored = sync.hydrate(a.session)
        print(json.dumps({"source": "AgentCore Memory", "restored": restored}, ensure_ascii=False))
        print(redact(store.render_markdown(store.load(a.session))))


def runtime_session(value: str) -> str:
    if not 33 <= len(value) <= 256:
        raise argparse.ArgumentTypeError("AgentCore session id must contain 33–256 characters")
    return value


def cmd_invoke(a):
    o = outputs()
    client = boto3.client("bedrock-agentcore", region_name=REGION,
                          config=__import__("botocore.config").config.Config(read_timeout=900))
    started = time.monotonic()
    resp = client.invoke_agent_runtime(agentRuntimeArn=o["RuntimeArn"], runtimeSessionId=a.session,
                                       payload=json.dumps({"prompt": a.prompt, "user_id": a.user,
                                                           "channel_id": a.channel, "stream": True}).encode(),
                                       contentType="application/json", accept="text/event-stream")
    result = None
    for line in resp["response"].iter_lines():
        line = line.decode() if isinstance(line, bytes) else line
        if not line.startswith("data:"):
            continue
        item = json.loads(line[5:].strip())
        if item.get("type") == "progress":
            print(f"  [{time.monotonic() - started:5.1f}s] … {item['message']}", flush=True)
        else:
            result = item
    print(f"\n=== result after {time.monotonic() - started:.1f}s ===")
    if not result:
        raise SystemExit("AgentCore stream ended without a result")
    if result.get("status") != "success":
        raise SystemExit(f"AgentCore invocation failed: {redact(str(result.get('error', 'unknown error')))}")
    print(result.get("response"))
    meta = result.get("meta") or {}
    print("\n--- meta ---")
    print(json.dumps({"route": result.get("route"), "approval_id": result.get("approval_id"),
                      "cost_usd": meta.get("cost_usd"), "elapsed_s": meta.get("elapsed_s"),
                      "evidence_created": meta.get("evidence_created"), "memory": meta.get("memory"),
                      "hook_denials": meta.get("hook_denials"),
                      "gateway_calls": [{k: g.get(k) for k in ("gateway_tool", "outcome", "ms", "error")}
                                        for g in meta.get("gateway_calls") or []]}, ensure_ascii=False, indent=1))
    mfix = meta.get("manual_fix") or {}
    if mfix:
        fix = mfix.get("fix") or {}
        print("\n--- manual_fix ---")
        print(json.dumps({"error": mfix.get("error"), "cost_usd": mfix.get("cost_usd"),
                          "steps": [f"{i['ref']} {'RO' if i['read_only'] else 'CHG'}{' ⚠️' if i.get('destructive') else ''} "
                                    f"{(i.get('command') or i.get('console_path'))[:100]}"
                                    for k in ("steps", "verification", "rollback") for i in fix.get(k, [])],
                          "doc_urls": fix.get("doc_urls"), "issues": mfix.get("issues")}, ensure_ascii=False, indent=1))
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=1))


def cmd_gateway(a):
    o = outputs()
    targets = parse_targets(o["GatewayTargets"])
    targets.update({"security_group_open_to_world": "forbidden", "ssm_run_any_automation": "forbidden"})
    gw = GatewayToolClient(o["GatewayUrl"], targets, region=REGION)
    ident = boto3.client("sts", region_name=REGION).get_caller_identity()["Arn"]
    print("caller principal:", redact(ident))
    print("tools/list (filtered by policy for this principal):", gw.list_tools())
    cases = [
        ("cloudwatch_metric_summary", {"service": "checkout-api", "metric": "5xx_rate"}, "permit (read tool)"),
        ("security_group_open_to_world", {"group_id": "sg-0123456789abcdef0", "port": 22}, "forbid (decoy)"),
        ("ssm_run_any_automation", {"document": "AWS-RunShellScript"}, "forbid (decoy)"),
        ("runbook_propose", {"runbook_id": "restart-ec2-instance", "params": {"service": "checkout-api",
                                                                               "InstanceId": "i-0a1b2c3d4e5f60001"},
                             "reason": "direct call from operator should be denied", "evidence_ids": ["EV-01"],
                             "caller": {"thread_id": "probe", "user_id": "probe"}},
         "deny (principal is not the runtime role)"),
    ]
    for tool, args, expect in cases:
        env, info = gw.call_sync(tool, args)
        print(f"\n# {tool}  expected: {expect}")
        print("  outcome:", info.get("outcome"), "| ok:", env.get("ok"), "| ms:", info.get("ms"))
        print("  detail :", (env.get("summary") or env.get("error") or "")[:300])
    docs = GatewayDocsClient(gw, o.get("DocsTarget", "awsdocs"))
    for remote, args, expect in [
        ("aws___search_documentation", {"search_phrase": "RDS modify-db-cluster-parameter-group max_connections",
                                        "limit": 2}, "permit (AWS Knowledge MCP target)"),
        ("aws___list_regions", {}, "deny (not in permit_aws_docs_read, default deny)"),
    ]:
        print(f"\n# {docs.target}___{remote}  expected: {expect}")
        try:
            if remote not in ("aws___search_documentation", "aws___read_documentation"):
                res = gw.rpc("tools/call", {"name": f"{docs.target}___{remote}", "arguments": args})
                text = "".join(c.get("text", "") for c in (res.get("result") or {}).get("content", []))
                print("  outcome:", "denied" if "denied" in (text + json.dumps(res.get("error"))).lower() else "?",
                      "| detail:", (text or json.dumps(res.get("error")))[:200])
                continue
            text, info = docs.call_sync(remote, args)
            print("  outcome:", info.get("outcome"), "| ms:", info.get("ms"),
                  "| urls:", [r["url"] for r in parse_search(text)])
        except DocsError as e:
            print("  outcome:", e.outcome, "| detail:", e.message[:200])


def _signed_post(url: str, body: bytes, content_type: str, secret: str, ts: str | None = None):
    ts = ts or str(int(time.time()))
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": content_type, "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": compute_signature(secret, ts, body)})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, (time.monotonic() - started) * 1000, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, (time.monotonic() - started) * 1000, e.read().decode()


def _secret() -> str:
    sm = boto3.client("secretsmanager", region_name=REGION)
    return json.loads(sm.get_secret_value(SecretId=outputs()["SlackSecretArn"])["SecretString"])["signing_secret"]


def cmd_slack_event(a):
    o = outputs()
    ts = a.thread_ts or f"{time.time():.6f}"
    event_id = a.event_id or f"Ev{int(time.time() * 1000)}"
    body = json.dumps({"type": "event_callback", "team_id": "T0AIOPS", "api_app_id": "A0AIOPS", "event_id": event_id,
                       "event_time": int(time.time()),
                       "event": {"type": "app_mention", "user": a.user, "channel": a.channel, "ts": ts,
                                 "thread_ts": a.thread_ts or None, "text": f"<@U0AIOPSBOT> {a.text}",
                                 "client_msg_id": event_id}}).encode()
    secret = "WRONG-SECRET" if a.bad_signature else _secret()
    status, ms, text = _signed_post(o["SlackEventsUrl"], body, "application/json", secret)
    print(json.dumps({"status": status, "latency_ms": round(ms), "body": text[:200], "event_id": event_id,
                      "channel": a.channel, "thread_ts": a.thread_ts or ts,
                      "session": f"slack-thread-{a.channel}-{a.thread_ts or ts}"}, ensure_ascii=False))


def _approval_item(approval_id: str) -> dict:
    from aiops.cloud.stores import from_ddb

    ddb = boto3.client("dynamodb", region_name=REGION)
    item = ddb.get_item(TableName=outputs()["TableName"], Key={"pk": {"S": f"APR#{approval_id}"}},
                        ConsistentRead=True).get("Item")
    return from_ddb(item) if item else {}


def cmd_slack_click(a):
    o = outputs()
    item = _approval_item(a.approval_id)
    if not item:
        sys.exit(f"no approval {a.approval_id}")
    session = item["thread_id"]
    thread_ts = session.rsplit("-", 1)[-1]
    payload = {"type": "block_actions", "user": {"id": a.user}, "channel": {"id": item.get("channel_id")},
               "message": {"ts": a.card_ts or item.get("card_ts"), "thread_ts": thread_ts, "blocks": []},
               "actions": [{"action_id": "aiops_approve" if a.action == "approve" else "aiops_reject",
                            "value": a.approval_id, "type": "button"}],
               "response_url": "https://hooks.slack.com/actions/DRYRUN"}
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    status, ms, text = _signed_post(o["SlackInteractivityUrl"], body, "application/x-www-form-urlencoded", _secret())
    after = _approval_item(a.approval_id)
    print(json.dumps({"status": status, "latency_ms": round(ms), "user": a.user, "action": a.action,
                      "record_status_after": after.get("status"), "decided_by": after.get("decided_by")},
                     ensure_ascii=False))


def cmd_approval(a):
    item = _approval_item(a.approval_id)
    item.pop("task_token", None)
    print(redact(json.dumps(item, ensure_ascii=False, indent=1, default=str)))


def cmd_reset(_):
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.delete_item(TableName=outputs()["TableName"], Key={"pk": {"S": "MOCKSTATE#default"}})
    print("mock world reset (MOCKSTATE#default deleted). Approval history is kept; new threads start clean.")


def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("outputs")
    sub.add_parser("status")
    i = sub.add_parser("invoke")
    i.add_argument("prompt")
    i.add_argument("--session", required=True, type=runtime_session)
    i.add_argument("--user", default="U0AIOPSREQUESTER")
    i.add_argument("--channel", default="C0AIOPSDEMO")
    i.add_argument("--json-out")
    sub.add_parser("gateway")
    n = sub.add_parser("notes")
    n.add_argument("--session", required=True, type=runtime_session)
    e = sub.add_parser("slack-event")
    e.add_argument("text")
    e.add_argument("--channel", default="C0AIOPSDEMO")
    e.add_argument("--thread-ts")
    e.add_argument("--user", default="U0AIOPSREQUESTER")
    e.add_argument("--event-id")
    e.add_argument("--bad-signature", action="store_true")
    c = sub.add_parser("slack-click")
    c.add_argument("approval_id")
    c.add_argument("--user", required=True)
    c.add_argument("--action", default="approve", choices=["approve", "reject"])
    c.add_argument("--card-ts")
    ap = sub.add_parser("approval")
    ap.add_argument("approval_id")
    sub.add_parser("reset")
    a = p.parse_args(argv)
    {"outputs": cmd_outputs, "status": cmd_status, "notes": cmd_notes,
     "invoke": cmd_invoke, "gateway": cmd_gateway, "slack-event": cmd_slack_event,
     "slack-click": cmd_slack_click, "approval": cmd_approval, "reset": cmd_reset}[a.cmd](a)


if __name__ == "__main__":
    main()
