"""Slack handlers tested with a fake UI and with real Bolt dispatch (no tokens, no network)."""

import pytest
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.request.async_request import AsyncBoltRequest

from aiops.formatting import APPROVE_ACTION
from aiops.slack_app import SlackBot

from .conftest import APPROVER, OUTSIDER, REQUESTER
from .test_pipeline import change_fake


class FakeUI:
    def __init__(self):
        self.posts, self.updates, self.ephemerals = [], [], []
        self._n = 0

    async def post(self, channel, thread_ts, text, blocks=None):
        self._n += 1
        ts = f"1700000100.{self._n:06d}"
        self.posts.append({"channel": channel, "thread_ts": thread_ts, "text": text, "blocks": blocks, "ts": ts})
        return ts

    async def update(self, channel, ts, text, blocks=None):
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})

    async def ephemeral(self, channel, user, text, thread_ts=None):
        self.ephemerals.append({"channel": channel, "user": user, "text": text})


@pytest.fixture
def ui():
    return FakeUI()


@pytest.fixture
def bot(pipeline, ui):
    return SlackBot(pipeline, ui, progress_interval=0)


def mention(text, ts="1700000000.000100", thread_ts=None, user=REQUESTER, **extra):
    ev = {"type": "app_mention", "user": user, "text": f"<@UBOT> {text}", "ts": ts, "channel": "C1",
          "event_ts": ts, "client_msg_id": f"m-{ts}-{text[:5]}"}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    ev.update(extra)
    return ev


async def test_mention_acks_fast_then_replaces_with_answer(bot, ui, pipeline):
    await bot.on_message_event(mention("어제 대비 비용이 튄 서비스는?"))
    first = ui.posts[0]
    assert first["text"].startswith("비용 리포트 생성 중") and first["thread_ts"] == "1700000000.000100"
    final = ui.updates[-1]
    assert final["ts"] == first["ts"] and "EC2 - Other" in final["text"]
    # thread = session: notes are keyed by channel + thread ts
    assert pipeline.notes.load("C1-1700000000.000100")["reports"]


async def test_followup_in_same_thread_reuses_session(bot, ui, pipeline):
    await bot.on_message_event(mention("알람 현황"))
    await bot.on_message_event(mention("비용 리포트", ts="1700000000.000200", thread_ts="1700000000.000100"))
    notes = pipeline.notes.load("C1-1700000000.000100")
    assert [r["kind"] for r in notes["reports"]] == ["alarm_report", "cost_report"]


def test_event_filtering(bot):
    assert bot.accept_event(mention("x"))
    assert not bot.accept_event(mention("x"))  # Slack retry (same client_msg_id) is dropped
    assert not bot.accept_event(mention("y", bot_id="B123"))  # bot loop prevention
    assert not bot.accept_event({"type": "message", "channel_type": "channel", "text": "hi", "ts": "1", "channel": "C"})
    assert bot.accept_event({"type": "message", "channel_type": "im", "text": "hi", "ts": "2", "channel": "D1"})
    assert not bot.accept_event({"type": "message", "subtype": "message_changed", "channel_type": "im", "ts": "3",
                                 "channel": "D1"})


async def _card(bot, ui, pipeline, runner):
    runner.on("change", change_fake())
    await bot.on_message_event(mention("재시작해줘"))
    final = ui.updates[-1]
    approval_id = next(b for b in final["blocks"] if b["type"] == "actions")["elements"][0]["value"]
    rec = pipeline.approvals.store.get(approval_id)
    assert rec.card_ts == ui.posts[0]["ts"]  # card bound to its message
    return rec, final


def action_body(approval_id, user, card_ts, blocks, channel="C1"):
    return {"type": "block_actions", "user": {"id": user}, "channel": {"id": channel}, "team": {"id": "T1"},
            "message": {"ts": card_ts, "thread_ts": "1700000000.000100", "blocks": blocks},
            "actions": [{"action_id": APPROVE_ACTION, "value": approval_id, "type": "button"}]}


async def test_non_approver_click_is_refused(bot, ui, pipeline, runner):
    rec, final = await _card(bot, ui, pipeline, runner)
    await bot.on_approval_action(action_body(rec.id, OUTSIDER, rec.card_ts, final["blocks"]), "approve")
    assert ui.ephemerals and "not_approver" in ui.ephemerals[-1]["text"]
    assert pipeline.approvals.store.get(rec.id).status == "PENDING"


async def test_approver_click_executes_and_verifies(bot, ui, pipeline, runner):
    rec, final = await _card(bot, ui, pipeline, runner)
    await bot.on_approval_action(action_body(rec.id, APPROVER, rec.card_ts, final["blocks"]), "approve")
    rec = pipeline.approvals.store.get(rec.id)
    assert rec.status == "SUCCEEDED" and rec.verify["result"] == "PASS"
    card_update = ui.updates[-1]
    assert not any(b["type"] == "actions" for b in card_update["blocks"])  # buttons removed
    texts = [p["text"] for p in ui.posts]
    assert any("승인했습니다" in t for t in texts) and any("실행 시작" in t for t in texts)
    assert any("검증 통과" in t for t in texts)
    # a second click after execution is refused
    await bot.on_approval_action(action_body(rec.id, APPROVER, rec.card_ts, final["blocks"]), "approve")
    assert "already_decided" in ui.ephemerals[-1]["text"]


async def test_button_replayed_from_another_message_is_refused(bot, ui, pipeline, runner):
    rec, final = await _card(bot, ui, pipeline, runner)
    await bot.on_approval_action(action_body(rec.id, APPROVER, "1700000999.000001", final["blocks"]), "approve")
    assert "wrong_context" in ui.ephemerals[-1]["text"]
    assert pipeline.approvals.store.get(rec.id).status == "PENDING"


# ------------------------------------------------------------------ real Bolt dispatch
async def _authorize(**kwargs):
    return AuthorizeResult(enterprise_id=None, team_id="T1", bot_user_id="UBOT", bot_id="BBOT", bot_token="xoxb-test")


@pytest.fixture
def app(bot):
    app = AsyncApp(authorize=_authorize, signing_secret="unused", request_verification_enabled=False)
    bot.register(app)
    return app


async def wait_until(cond, timeout=3.0):
    import asyncio

    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")


async def test_bolt_dispatch_app_mention(app, bot, ui):
    body = {"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event_id": "Ev1", "event_time": 1,
            "event": mention("알람 현황"),
            "authorizations": [{"enterprise_id": None, "team_id": "T1", "user_id": "UBOT", "is_bot": True}]}
    resp = await app.async_dispatch(AsyncBoltRequest(body=body, mode="socket_mode"))
    assert resp.status == 200  # acked before the listener runs (Bolt runs listeners after ack)
    await wait_until(lambda: ui.updates)
    await bot.drain()
    assert ui.posts and "알람 현황" in ui.updates[-1]["text"]


async def test_bolt_dispatch_button_acks_and_checks_approver(app, bot, ui, pipeline, runner):
    rec, final = await _card(bot, ui, pipeline, runner)
    body = action_body(rec.id, OUTSIDER, rec.card_ts, final["blocks"]) | {"api_app_id": "A1", "trigger_id": "t",
                                                                         "container": {"type": "message"}}
    body["actions"][0].update({"block_id": f"approval:{rec.id}", "action_ts": "1"})
    resp = await app.async_dispatch(AsyncBoltRequest(body=body, mode="socket_mode"))
    assert resp.status == 200
    await wait_until(lambda: ui.ephemerals)
    await bot.drain()
    assert pipeline.approvals.store.get(rec.id).status == "PENDING"
    assert "not_approver" in ui.ephemerals[-1]["text"]
