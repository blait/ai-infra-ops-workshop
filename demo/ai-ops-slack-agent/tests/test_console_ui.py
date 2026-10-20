from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest


def test_chat_renders_long_reply_and_modes_use_separate_sessions(monkeypatch):
    from aiops import console

    calls = []
    class FakeConsole:
        def status(self):
            return {"ready": True, "checks": {k: True for k in ["Runtime", "Gateway", "Policy", "Memory", "AWS Toolkit"]},
                    "runtime_version": "test", "region": "us-east-1"}
        def invoke(self, prompt, sid, mode, progress):
            calls.append((sid, mode))
            progress("테스트 근거 수집")
            return {"status": "success", "response": ("테스트 응답 " * 350) + "\n[근거]\n• EV-01\n"
                    "======== 수동 조치 초안 · 검증 필요 ========\n실행하지 않음",
                    "route": "investigate", "meta": {"elapsed_s": 1, "cost_usd": 0.01}}
    monkeypatch.setattr(console, "AwsConsole", FakeConsole)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py")).run(timeout=15)
    assert not app.exception
    next(b for b in app.button if b.label == "장애 원인 조사").click().run(timeout=15)
    assert not app.exception and len(app.chat_message) == 2
    assert any(e.label.startswith("수동 조치") for e in app.expander)
    app.radio[0].set_value("실제 AWS 계정").run(timeout=15)
    assert not app.exception and len(app.chat_message) == 0
    next(b for b in app.button if b.label == "EC2 운영 현황").click().run(timeout=15)
    assert not app.exception and calls[0][0] != calls[1][0]
    assert [mode for _, mode in calls] == ["scenario", "aws_live"]
