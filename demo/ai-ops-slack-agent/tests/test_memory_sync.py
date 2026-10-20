import json

from aiops.memory import AgentCoreMemory, MemoryActionSink, NotesMemorySync, memory_session_id
from aiops.notes import NotesStore


class FakeMemoryClient:
    def __init__(self, page_size=2):
        self.events: list[dict] = []
        self.page_size = page_size
        self.fail = False

    def create_event(self, **kw):
        if self.fail:
            raise RuntimeError("boom")
        ev = {"eventId": f"e{len(self.events)}", **kw}
        self.events.append(ev)
        return {"event": {"eventId": ev["eventId"]}}

    def list_events(self, memoryId, actorId, sessionId, includePayloads, maxResults, nextToken=None):
        mine = [e for e in self.events if e["sessionId"] == sessionId and e["actorId"] == actorId]
        start = int(nextToken or 0)
        page = mine[start:start + self.page_size]
        out = {"events": page}
        if start + self.page_size < len(mine):
            out["nextToken"] = str(start + self.page_size)
        return out


def _evidence(store, thread, summary):
    return store.add_evidence(thread, tool="logs_error_summary", args={"service": "x"}, summary=summary,
                              render=lambda ev: json.dumps({"evidence_id": ev, "summary": summary}))


def test_memory_session_id_sanitizes():
    assert memory_session_id("slack-thread-C01-1737849234.123456") == "slack-thread-C01-1737849234_123456"
    assert memory_session_id("_x.y")[0].isalnum()
    assert memory_session_id("...") == "default"
    assert len(memory_session_id("a" * 300)) == 100


def test_persist_then_hydrate_into_fresh_store(tmp_path):
    client = FakeMemoryClient()
    mem = AgentCoreMemory("mem-abcdefghij", "us-east-1", client=client)
    src = NotesStore(tmp_path / "a")
    thread = "slack-thread-C1-1.2"
    src.add_request(thread, "U1", "investigate", "5xx 원인")
    ev1, payload1 = _evidence(src, thread, "first")
    ev2, _ = _evidence(src, thread, "second")
    info = NotesMemorySync(mem, src).persist(thread, new_evidence=[ev1, ev2], request="5xx 원인", reply="답", user_id="U1")
    assert info == {"evidence_events": 2, "snapshot": 1, "turns": 2}
    kinds = [e["metadata"]["kind"]["stringValue"] for e in client.events]
    assert kinds == ["evidence", "evidence", "notes", "turns"]
    roles = [p["conversational"]["role"] for p in client.events[-1]["payload"]]
    assert roles == ["USER", "ASSISTANT"]
    assert all(e["sessionId"] == "slack-thread-C1-1_2" and e["actorId"] == "aiops" for e in client.events)

    # Lambda side writes action events
    sink = MemoryActionSink(mem)
    sink.upsert_action(thread, "apr-1", status="PENDING", runbook="restart-ec2-instance")
    sink.upsert_action(thread, "apr-1", status="APPROVED", decided_by="U9")
    sink.upsert_action(thread, "apr-1", status="SUCCEEDED", verify="PASS")

    dst = NotesStore(tmp_path / "b")
    got = NotesMemorySync(mem, dst).hydrate(thread)
    assert got == {"events": 7, "snapshots": 1, "evidence": 2, "actions": 3}
    notes = dst.load(thread)
    assert notes["requests"][0]["text"] == "5xx 원인"
    assert dst.evidence_ids(thread) == {ev1, ev2}
    assert dst.get_evidence(thread, ev1)["payload"] == payload1
    act = notes["actions"][0]
    assert act["status"] == "SUCCEEDED" and act["verify"] == "PASS" and act["decided_by"] == "U9"
    assert "조치 apr-1" in dst.render_for_prompt(thread)


def test_hydrate_empty_session(tmp_path):
    mem = AgentCoreMemory("mem-abcdefghij", "us-east-1", client=FakeMemoryClient())
    info = NotesMemorySync(mem, NotesStore(tmp_path)).hydrate("new-thread")
    assert info == {"events": 0, "snapshots": 0, "evidence": 0, "actions": 0}


def test_list_events_resource_not_found_is_empty():
    class C(FakeMemoryClient):
        def list_events(self, **kw):
            raise type("ResourceNotFoundException", (Exception,), {})("no session")

    assert AgentCoreMemory("mem-abcdefghij", "us-east-1", client=C()).list_events("t") == []


def test_hydrate_ignores_bad_evidence_ids(tmp_path):
    client = FakeMemoryClient()
    mem = AgentCoreMemory("mem-abcdefghij", "us-east-1", client=client)
    mem.put_blob("t", {"kind": "evidence", "seq": 1, "evidence": {"id": "../../etc", "payload": "x"}}, "evidence")
    store = NotesStore(tmp_path)
    assert NotesMemorySync(mem, store).hydrate("t")["evidence"] == 1
    assert not (store.thread_dir("t") / "evidence").exists() or not list((store.thread_dir("t") / "evidence").iterdir())


def test_action_sink_noop_and_swallows_errors(capsys):
    MemoryActionSink(None).upsert_action("t", "apr-1", status="X")
    assert MemoryActionSink(None).evidence_ids("t") == set()
    client = FakeMemoryClient()
    client.fail = True
    MemoryActionSink(AgentCoreMemory("mem-abcdefghij", "us-east-1", client=client)).upsert_action("t", "apr-1", status="X")
    assert "memory_action_write_failed" in capsys.readouterr().out


def test_blob_is_sent_as_json_string_and_java_map_form_is_ignored(tmp_path):
    """Live finding (2026-10-18): a dict sent as `blob` comes back from ListEvents as a Java
    Map.toString() string ('{kind=notes, ...}'), which is not JSON. We send a JSON string instead."""
    client = FakeMemoryClient()
    mem = AgentCoreMemory("mem-1234567890", "us-east-1", client=client)
    mem.put_blob("t-1", {"kind": "notes", "seq": 1, "notes": {"a": "x, y={z}"}}, "notes")
    blob = client.events[-1]["payload"][0]["blob"]
    assert isinstance(blob, str) and json.loads(blob)["notes"]["a"] == "x, y={z}"
    from aiops.memory import _blobs

    lossy = [{"payload": [{"blob": "{kind=notes, seq=1, notes={a=x}}"}]}]
    assert _blobs(lossy) == []
