import time

import pytest

from aiops.cloud import slack as sl

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"


@pytest.fixture(autouse=True)
def _clear():
    sl.clear_cache()
    yield
    sl.clear_cache()


def test_signature_matches_slack_documented_example():
    # Example from https://api.slack.com/authentication/verifying-requests-from-slack
    body = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V"
            b"&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text="
            b"&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
            b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c")
    assert sl.compute_signature(SECRET, "1531420618", body) == \
        "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"


def test_verify_signature_cases():
    now = time.time()
    ts = str(int(now))
    body = b'{"a":1}'
    good = sl.compute_signature("s3cret", ts, body)
    assert sl.verify_signature("s3cret", ts, body, good, now=now) == (True, "ok")
    assert sl.verify_signature("s3cret", ts, body + b" ", good, now=now)[1] == "signature mismatch"
    assert "stale" in sl.verify_signature("s3cret", ts, body, good, now=now + 301)[1]
    assert sl.verify_signature("s3cret", None, body, good)[1] == "missing signature headers"
    assert sl.verify_signature("s3cret", "abc", body, good)[1] == "bad timestamp"
    assert sl.verify_signature("REPLACE_ME", ts, body, good)[0] is False
    assert sl.verify_signature("", ts, body, good)[0] is False


def test_dry_run_logs_and_fakes_ts():
    logs = []
    c = sl.SlackClient("xoxb-real", dry_run=True, log=logs.append)
    ts = c.post("C1", "1.2", "hello", blocks=[{"type": "divider"}])
    assert ts and float(ts) > 0
    assert logs[0]["slack_dry_run"]["method"] == "chat.postMessage"
    assert logs[0]["slack_dry_run"]["channel"] == "C1" and logs[0]["slack_dry_run"]["text"] == "hello"
    c.update("C1", "9.9", "x")
    assert logs[-1]["slack_dry_run"]["ts"] == "9.9"
    c.ephemeral("C1", "U1", "nope", "1.2")
    assert logs[-1]["slack_dry_run"]["method"] == "chat.postEphemeral"


def test_placeholder_or_missing_token_forces_dry_run():
    assert sl.SlackClient("REPLACE_ME", dry_run=False).dry_run is True
    assert sl.SlackClient(None, dry_run=False).dry_run is True
    assert sl.SlackClient("xoxb-1", dry_run=False).dry_run is False


def test_real_call_posts_with_bearer_token():
    seen = []

    class Resp:
        def read(self):
            return b'{"ok": true, "ts": "123.4"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=None):
        seen.append(req)
        return Resp()

    c = sl.SlackClient("xoxb-1", dry_run=False, opener=opener)
    assert c.post("C1", None, "hi") == "123.4"
    assert seen[0].full_url.endswith("/chat.postMessage")
    assert dict(seen[0].header_items())["Authorization"] == "Bearer xoxb-1"


class FakeSsm:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def get_parameter(self, Name):
        self.calls += 1
        return {"Parameter": {"Value": self.value}}


def test_load_approvers_filters_placeholders_and_caches():
    ssm = FakeSsm("U1, REPLACE_WITH_ID ,U2,")
    assert sl.load_approvers("/p", client=ssm) == frozenset({"U1", "U2"})
    assert sl.load_approvers("/p", client=ssm) == frozenset({"U1", "U2"})
    assert ssm.calls == 1
    sl.clear_cache()
    assert sl.load_approvers("/p", client=FakeSsm("REPLACE_ME")) == frozenset()


def test_load_slack_secret_caches():
    class Sm:
        calls = 0

        def get_secret_value(self, SecretId):
            Sm.calls += 1
            return {"SecretString": '{"bot_token": "xoxb-1", "signing_secret": "s"}'}

    assert sl.load_slack_secret("arn:x", client=Sm())["signing_secret"] == "s"
    sl.load_slack_secret("arn:x", client=Sm())
    assert Sm.calls == 1
