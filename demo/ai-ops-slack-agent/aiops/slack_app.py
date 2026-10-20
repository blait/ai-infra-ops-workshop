"""Slack front end (slack_bolt AsyncApp in Socket Mode: no public endpoint needed).

Patterns (research/03 §3)
- Fast ack: Bolt acks the event immediately; we post "조사 중…" in the thread right away and run the
  agent in a background task, then replace that message with the answer (chat.update).
- Thread = session: session id = "<channel>-<thread_ts>"; the thread's structured notes file is the
  agent's memory between messages.
- Approval outside the model: the [승인]/[거절] buttons carry only the approval id; the handler
  checks the clicking user against AIOPS_APPROVERS and the record in SQLite, then runs the executor
  and the verify step. Non-approvers get an ephemeral refusal and nothing changes.
- Bot-loop prevention: Bolt ignores our own events; we also drop bot_id/subtype messages and
  de-duplicate Slack retries.

Run:  python -m aiops.slack_app   (needs SLACK_BOT_TOKEN, SLACK_APP_TOKEN, AIOPS_APPROVERS)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any

from .errors import ApprovalError
from .formatting import APPROVE_ACTION, REJECT_ACTION, context, verify_text
from .pipeline import OpsPipeline
from .router import classify

log = logging.getLogger("aiops.slack")
_MENTION = re.compile(r"<@[A-Z0-9]+(\|[^>]+)?>")
_ROUTE_ACK = {
    "investigate": "조사 중… 지표·로그·변경 이력을 모으고 있습니다",
    "change_request": "변경 요청 확인 중… 허용 목록 runbook과 근거를 대조합니다",
    "cost_report": "비용 리포트 생성 중…",
    "alarm_report": "알람 현황 조회 중…",
}


class SlackUI:
    """Thin wrapper over AsyncWebClient so handlers are testable with a fake."""

    def __init__(self, client):
        self.client = client

    async def post(self, channel: str, thread_ts: str | None, text: str, blocks: list | None = None) -> str:
        resp = await self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text[:3900],
                                                  blocks=blocks, unfurl_links=False)
        return resp["ts"]

    async def update(self, channel: str, ts: str, text: str, blocks: list | None = None) -> None:
        await self.client.chat_update(channel=channel, ts=ts, text=text[:3900], blocks=blocks or [])

    async def ephemeral(self, channel: str, user: str, text: str, thread_ts: str | None = None) -> None:
        await self.client.chat_postEphemeral(channel=channel, user=user, text=text, thread_ts=thread_ts)


class ThrottledStatus:
    """Edits the ack message with the latest progress lines, at most every `interval` seconds."""

    def __init__(self, ui: SlackUI, channel: str, ts: str, head: str, interval: float = 2.5):
        self.ui, self.channel, self.ts, self.head, self.interval = ui, channel, ts, head, interval
        self.lines: list[str] = []
        self._last = 0.0

    async def __call__(self, message: str) -> None:
        self.lines = (self.lines + [message])[-4:]
        now = time.monotonic()
        if now - self._last < self.interval:
            return
        self._last = now
        body = self.head + "\n" + "\n".join(f"• {m}" for m in self.lines)
        try:
            await self.ui.update(self.channel, self.ts, body)
        except Exception:  # noqa: BLE001 - progress is best-effort (rate limits etc.)
            log.debug("progress update failed", exc_info=True)


class SlackBot:
    def __init__(self, pipeline: OpsPipeline, ui: SlackUI, progress_interval: float = 2.5):
        self.pipeline = pipeline
        self.ui = ui
        self.progress_interval = progress_interval
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ helpers
    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.error("background task failed", exc_info=t.exception())

        task.add_done_callback(done)
        return task

    async def drain(self) -> None:
        """Wait for background tasks (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _dedupe(self, key: str) -> bool:
        if key in self._seen:
            return True
        self._seen[key] = None
        while len(self._seen) > 500:
            self._seen.popitem(last=False)
        return False

    @staticmethod
    def session_id(channel: str, thread_ts: str) -> str:
        return f"{channel}-{thread_ts}"

    # ------------------------------------------------------------------ messages
    def accept_event(self, event: dict) -> bool:
        if event.get("bot_id") or event.get("subtype") not in (None, "thread_broadcast"):
            return False
        if event.get("type") == "message" and event.get("channel_type") != "im":
            return False  # channel messages arrive as app_mention
        key = event.get("client_msg_id") or f"{event.get('channel')}:{event.get('ts')}"
        return not self._dedupe(key)

    async def on_message_event(self, event: dict) -> None:
        channel = event["channel"]
        user = event.get("user", "unknown")
        thread_ts = event.get("thread_ts") or event["ts"]
        text = _MENTION.sub("", event.get("text", "")).strip()
        route = classify(text)
        head = _ROUTE_ACK.get(route.kind, "확인 중…")
        ack_ts = await self.ui.post(channel, thread_ts, head)
        status = ThrottledStatus(self.ui, channel, ack_ts, head, self.progress_interval)
        reply = await self.pipeline.handle(thread_id=self.session_id(channel, thread_ts), user_id=user, text=text,
                                           channel_id=channel, progress=status)
        await self.ui.update(channel, ack_ts, reply.text, reply.blocks)
        if reply.approval_id:
            # the approval card is this message: bind the record to it (button replay protection)
            self.pipeline.approvals.attach_card(reply.approval_id, channel, ack_ts)

    # ------------------------------------------------------------------ approval buttons
    async def on_approval_action(self, body: dict, decision: str) -> None:
        action = body["actions"][0]
        approval_id = action["value"]
        user = body["user"]["id"]
        channel = body["channel"]["id"]
        message = body.get("message") or {}
        card_ts = message.get("ts")
        thread_ts = message.get("thread_ts") or card_ts
        try:
            rec = self.pipeline.decide(approval_id, user, decision, channel_id=channel, card_ts=card_ts)
        except ApprovalError as e:
            await self.ui.ephemeral(channel, user, f"처리하지 않았습니다 [{e.code}]: {e.message}", thread_ts)
            return
        label = "승인" if rec.status == "APPROVED" else "거절"
        kept = [b for b in message.get("blocks", []) if b.get("type") != "actions"]
        await self.ui.update(channel, card_ts, f"{label}: {approval_id}",
                             kept + [context(f"*{label}됨* · <@{user}> · 상태 `{rec.status}`")])
        if rec.status != "APPROVED":
            await self.ui.post(channel, thread_ts, f"<@{user}>님이 `{approval_id}`를 거절했습니다. 실행하지 않습니다.")
            return
        await self.ui.post(channel, thread_ts, f"<@{user}>님이 `{approval_id}`를 승인했습니다. 실행기가 실행합니다(허용 목록 문서만).")

        async def progress(msg: str) -> None:
            await self.ui.post(channel, thread_ts, msg)

        try:
            final = await self.pipeline.execute(approval_id, progress=progress)
        except ApprovalError as e:
            await self.ui.post(channel, thread_ts, f"실행하지 않았습니다 [{e.code}]: {e.message}")
            return
        await self.ui.post(channel, thread_ts, verify_text(final))
        await self.ui.update(channel, card_ts, f"{label}: {approval_id}",
                             kept + [context(f"*{label}됨* · <@{user}> · 최종 `{final.status}` · 검증 "
                                             f"`{(final.verify or {}).get('result', '-')}` · 실행 `{final.execution_id}`")])

    # ------------------------------------------------------------------ bolt wiring
    def register(self, app) -> None:
        @app.event("app_mention")
        async def _on_mention(event: dict) -> None:
            if self.accept_event(event):
                self.spawn(self.on_message_event(event))

        @app.event("message")
        async def _on_message(event: dict) -> None:
            if self.accept_event(event):
                self.spawn(self.on_message_event(event))

        @app.action(APPROVE_ACTION)
        async def _on_approve(ack, body: dict) -> None:
            await ack()
            self.spawn(self.on_approval_action(body, "approve"))

        @app.action(REJECT_ACTION)
        async def _on_reject(ack, body: dict) -> None:
            await ack()
            self.spawn(self.on_approval_action(body, "reject"))


def build_app(pipeline: OpsPipeline, *, token: str | None = None, **app_kwargs: Any):
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=token, **app_kwargs)
    bot = SlackBot(pipeline, SlackUI(app.client))
    bot.register(app)
    return app, bot


async def main() -> None:
    from dotenv import load_dotenv
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    from .config import PROJECT_ROOT, load_settings

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    logging.basicConfig(level=os.environ.get("AIOPS_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        raise SystemExit("SLACK_BOT_TOKEN(xoxb-)과 SLACK_APP_TOKEN(xapp-, connections:write)이 필요합니다. README 참고.")
    settings = load_settings()
    if not settings.approvers:
        log.warning("AIOPS_APPROVERS가 비어 있습니다: 모든 승인 클릭이 거부됩니다.")
    pipeline = OpsPipeline(settings)
    app, _ = build_app(pipeline, token=bot_token)
    log.info("starting Socket Mode (mode=%s, model=%s, approvers=%d)", settings.mode, settings.model,
             len(settings.approvers))
    await AsyncSocketModeHandler(app, app_token).start_async()


if __name__ == "__main__":
    asyncio.run(main())
