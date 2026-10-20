import json

import pytest

from aiops.notes import NotesStore, safe_thread_id


@pytest.fixture
def store(tmp_path):
    return NotesStore(tmp_path / "threads")


def add(store, thread, summary="s"):
    return store.add_evidence(thread, tool="logs_error_summary", args={"service": "x"}, summary=summary,
                              render=lambda ev: json.dumps({"evidence_id": ev}))


def test_evidence_ids_are_sequential_and_persisted(store, tmp_path):
    ids = [add(store, "C1-1.2")[0] for _ in range(3)]
    assert ids == ["EV-01", "EV-02", "EV-03"]
    # a new store instance (e.g. next Slack message / process restart) continues the sequence
    again = NotesStore(tmp_path / "threads")
    assert add(again, "C1-1.2")[0] == "EV-04"
    assert again.get_evidence("C1-1.2", "EV-02")["payload"] == json.dumps({"evidence_id": "EV-02"})
    assert again.get_evidence("C1-1.2", "../../etc/passwd") is None


def test_apply_rca_updates_timeline_ruled_out_and_hypothesis_history(store):
    add(store, "t")
    rca1 = {"service": "checkout-api", "summary": "s1", "hypothesis": "h1", "confidence": "medium",
            "evidence": [{"id": "EV-01", "fact": "f"}],
            "timeline": [{"at": "13:42", "event": "배포 v2.14.0", "evidence_id": "EV-01"}],
            "ruled_out": [{"item": "보안그룹 변경", "reason": "증상 이후", "evidence_ids": ["EV-01"]}],
            "next_checks": ["롤백 검토"], "missing_data": ["타깃별 5xx"]}
    store.apply_rca("t", rca1, reviewer_note="ok")
    rca2 = dict(rca1, hypothesis="h2", ruled_out=[{"item": "보안그룹 변경", "reason": "dup"},
                                                   {"item": "트래픽 급증", "reason": "x1.1"}])
    store.apply_rca("t", rca2)
    n = store.load("t")
    assert [h["status"] for h in n["hypotheses"]] == ["superseded", "active"]
    assert [r["item"] for r in n["ruled_out"]] == ["보안그룹 변경", "트래픽 급증"]
    assert len(n["timeline"]) == 1 and n["service"] == "checkout-api"
    md = (store.thread_dir("t") / "notes.md").read_text()
    assert "## 배제한 것" in md and "트래픽 급증" in md


def test_render_for_prompt_is_bounded_and_newest_first(store):
    for i in range(80):
        add(store, "t", summary=f"근거 {i} " + "x" * 80)
    digest = store.render_for_prompt("t", max_chars=1500)
    assert len(digest) <= 1500
    assert "EV-80" in digest and "EV-01" not in digest  # oldest dropped first
    assert "생략" in digest


def test_empty_thread_digest(store):
    assert "없음" in store.render_for_prompt("new-thread")


def test_actions_upsert(store):
    store.upsert_action("t", "apr-1", runbook="restart-ec2-instance", status="PENDING")
    store.upsert_action("t", "apr-1", status="APPROVED", decided_by="U1")
    acts = store.load("t")["actions"]
    assert len(acts) == 1 and acts[0]["status"] == "APPROVED" and acts[0]["runbook"] == "restart-ec2-instance"


def test_safe_thread_id():
    assert safe_thread_id("C123-1700000000.000100") == "C123-1700000000.000100"
    assert "/" not in safe_thread_id("../../x")
    with pytest.raises(ValueError):
        safe_thread_id("///")
