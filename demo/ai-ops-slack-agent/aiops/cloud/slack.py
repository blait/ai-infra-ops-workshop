"""Slack over HTTP for the Lambda side: request signature check, Web API calls, DRY_RUN.

Signature (https://api.slack.com/authentication/verifying-requests-from-slack):
  basestring = "v0:" + X-Slack-Request-Timestamp + ":" + raw body
  X-Slack-Signature == "v0=" + hex(HMAC-SHA256(signing_secret, basestring)), timestamp within 5 min.

DRY_RUN (AIOPS_SLACK_DRY_RUN=1, or the bot token is still the placeholder): no call to slack.com;
the would-be request is logged as one JSON line {"slack_dry_run": {...}} and a fake ts is returned,
so the whole Slack path can be verified before a Slack app exists.

Secrets: the bot token and signing secret live in one Secrets Manager secret (JSON keys
`bot_token`, `signing_secret`), read by the Lambdas that need them and cached for 5 minutes. The
agent runtime never gets this secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.request
from typing import Any, Callable

PLACEHOLDER_PREFIX = "REPLACE"
_CACHE: dict[str, tuple[float, Any]] = {}


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def verify_signature(secret: str, timestamp: str | None, body: bytes, signature: str | None,
                     now: float | None = None, tolerance_s: int = 300) -> tuple[bool, str]:
    if not secret or secret.startswith(PLACEHOLDER_PREFIX):
        return False, "signing secret not configured"
    if not timestamp or not signature:
        return False, "missing signature headers"
    try:
        ts = int(timestamp)
    except ValueError:
        return False, "bad timestamp"
    if abs((now if now is not None else time.time()) - ts) > tolerance_s:
        return False, "stale timestamp (replay window 5 min)"
    expected = compute_signature(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"
    return True, "ok"


def _cached(key: str, ttl_s: float, loader: Callable[[], Any]) -> Any:
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    value = loader()
    _CACHE[key] = (time.monotonic(), value)
    return value


def load_slack_secret(secret_arn: str, client=None) -> dict:
    def load():
        import boto3

        sm = client or boto3.client("secretsmanager")
        return json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])

    return _cached(f"secret:{secret_arn}", 300, load)


def load_approvers(param_name: str, client=None) -> frozenset[str]:
    """Approver Slack user ids from an SSM StringList parameter (outside the model, outside code)."""

    def load():
        import boto3

        ssm = client or boto3.client("ssm")
        raw = ssm.get_parameter(Name=param_name)["Parameter"]["Value"]
        return frozenset(x.strip() for x in raw.split(",") if x.strip() and not x.strip().startswith(PLACEHOLDER_PREFIX))

    return _cached(f"approvers:{param_name}", 60, load)


def clear_cache() -> None:
    _CACHE.clear()


class SlackClient:
    API = "https://slack.com/api/"

    def __init__(self, token: str | None, dry_run: bool, log: Callable[[dict], None] | None = None, opener=None):
        self.token = token or ""
        self.dry_run = dry_run or not self.token or self.token.startswith(PLACEHOLDER_PREFIX)
        self.log = log or (lambda rec: print(json.dumps(rec, ensure_ascii=False), flush=True))
        self._opener = opener or urllib.request.urlopen

    def call(self, method: str, **params: Any) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        if self.dry_run:
            fake_ts = f"{time.time():.6f}"
            self.log({"slack_dry_run": {"method": method, **params}, "fake_ts": fake_ts})
            return {"ok": True, "ts": params.get("ts") or fake_ts, "dry_run": True}
        req = urllib.request.Request(self.API + method, data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json; charset=utf-8",
                                              "Authorization": f"Bearer {self.token}"}, method="POST")
        with self._opener(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        if not body.get("ok"):
            self.log({"slack_error": {"method": method, "error": body.get("error")}})
        return body

    def post(self, channel: str, thread_ts: str | None, text: str, blocks: list | None = None) -> str:
        return self.call("chat.postMessage", channel=channel, thread_ts=thread_ts, text=text[:3900], blocks=blocks,
                         unfurl_links=False).get("ts", "")

    def update(self, channel: str, ts: str, text: str, blocks: list | None = None) -> None:
        self.call("chat.update", channel=channel, ts=ts, text=text[:3900], blocks=blocks or [])

    def ephemeral(self, channel: str, user: str, text: str, thread_ts: str | None = None) -> None:
        self.call("chat.postEphemeral", channel=channel, user=user, text=text, thread_ts=thread_ts)


def slack_from_env(log=None) -> SlackClient:
    arn = os.environ.get("AIOPS_SLACK_SECRET_ARN", "")
    token = load_slack_secret(arn).get("bot_token") if arn else None
    dry = os.environ.get("AIOPS_SLACK_DRY_RUN", "1").lower() in {"1", "true", "yes", "on"}
    return SlackClient(token, dry, log=log)
