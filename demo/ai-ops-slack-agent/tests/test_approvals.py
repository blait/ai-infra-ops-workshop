import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from aiops.errors import ApprovalError

from .conftest import APPROVER, APPROVER2, BAD_INSTANCE, I1, OUTSIDER, REQUESTER, seed_evidence

THREAD = "C1-1700000000.000100"


async def propose(pipeline, thread=THREAD, **overrides):
    ev = await seed_evidence(pipeline, thread)
    kw = dict(thread_id=thread, channel_id="C1", requested_by=REQUESTER, runbook_id="restart-ec2-instance",
              params={"service": "checkout-api", "InstanceId": I1},
              reason="i-...0001에만 커넥션 누수 경고와 풀 고갈 에러가 몰림", evidence_ids=ev[:1])
    kw.update(overrides)
    return pipeline.approvals.propose(**kw)


async def test_valid_proposal_is_pending_and_not_executed(pipeline):
    rec = await propose(pipeline)
    assert rec.status == "PENDING" and rec.document == "AWS-RestartEC2Instance"
    assert rec.execution_id is None
    assert json.loads(json.dumps(rec.params)) == {"InstanceId": I1, "service": "checkout-api"}


@pytest.mark.parametrize("overrides,code", [
    ({"runbook_id": "open-security-group"}, "not_allowlisted"),
    ({"runbook_id": "rollback-codedeploy-deployment", "params": {"service": "checkout-api", "DeploymentId": "d-ABCDEFGHI"}},
     "draft_runbook"),
    ({"params": {"service": "checkout-api", "InstanceId": "i-xyz; rm -rf /"}}, "bad_params"),
    ({"params": {"service": "checkout-api", "InstanceId": [I1, "i-0a1b2c3d4e5f60002"]}}, "bad_params"),
    ({"params": {"service": "checkout-api", "InstanceId": I1, "AutomationAssumeRole": "arn:x"}}, "bad_params"),
    ({"params": {"service": "checkout-api", "InstanceId": BAD_INSTANCE}}, "not_in_inventory"),
    ({"reason": "재시작"}, "no_reason"),
    ({"evidence_ids": []}, "no_evidence"),
    ({"evidence_ids": ["EV-77"]}, "unknown_evidence"),
])
async def test_invalid_proposals_rejected(pipeline, overrides, code):
    with pytest.raises(ApprovalError) as e:
        await propose(pipeline, **overrides)
    assert e.value.code == code
    assert pipeline.approvals.store.list() == []


async def test_one_change_at_a_time_per_thread_and_target(pipeline):
    await propose(pipeline)
    with pytest.raises(ApprovalError) as e:
        await propose(pipeline)
    assert e.value.code == "one_at_a_time"
    with pytest.raises(ApprovalError) as e:
        await propose(pipeline, thread="C1-other-thread")
    assert e.value.code == "target_busy"


async def test_non_approver_is_refused_and_record_unchanged(pipeline):
    rec = await propose(pipeline)
    with pytest.raises(ApprovalError) as e:
        pipeline.decide(rec.id, OUTSIDER, "approve", channel_id="C1")
    assert e.value.code == "not_approver"
    assert pipeline.approvals.store.get(rec.id).status == "PENDING"
    assert any(r["event"] == "approval_denied" and r["user_id"] == OUTSIDER for r in pipeline.audit.read())


async def test_double_approval_prevented_even_concurrently(pipeline):
    rec = await propose(pipeline)
    results, errors = [], []

    def click(user):
        try:
            results.append(pipeline.decide(rec.id, user, "approve", channel_id="C1"))
        except ApprovalError as e:
            errors.append(e.code)

    threads = [threading.Thread(target=click, args=(u,)) for u in (APPROVER, APPROVER2, APPROVER, APPROVER2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1 and errors == ["already_decided"] * 3
    assert pipeline.approvals.store.get(rec.id).status == "APPROVED"


async def test_wrong_channel_or_card_rejected(pipeline):
    rec = await propose(pipeline)
    pipeline.approvals.attach_card(rec.id, "C1", "1700000000.000200")
    with pytest.raises(ApprovalError) as e:
        pipeline.decide(rec.id, APPROVER, "approve", channel_id="C-OTHER")
    assert e.value.code == "wrong_context"
    with pytest.raises(ApprovalError) as e:
        pipeline.decide(rec.id, APPROVER, "approve", channel_id="C1", card_ts="1700000000.999999")
    assert e.value.code == "wrong_context"
    assert pipeline.decide(rec.id, APPROVER, "approve", channel_id="C1", card_ts="1700000000.000200").status == "APPROVED"


async def test_expired_approval(pipeline):
    rec = await propose(pipeline)
    pipeline.approvals.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=2)
    with pytest.raises(ApprovalError) as e:
        pipeline.decide(rec.id, APPROVER, "approve")
    assert e.value.code == "expired"
    assert pipeline.approvals.store.get(rec.id).status == "EXPIRED"


async def test_self_approval_blocked_when_required(pipeline):
    pipeline.approvals.require_distinct_approver = True
    rec = await propose(pipeline, requested_by=APPROVER)
    with pytest.raises(ApprovalError) as e:
        pipeline.decide(rec.id, APPROVER, "approve")
    assert e.value.code == "self_approval"
    assert pipeline.decide(rec.id, APPROVER2, "approve").status == "APPROVED"


async def test_reject_never_executes(pipeline):
    rec = await propose(pipeline)
    assert pipeline.decide(rec.id, APPROVER, "reject").status == "REJECTED"
    with pytest.raises(ApprovalError) as e:
        await pipeline.execute(rec.id)
    assert e.value.code == "not_approved"


async def test_execute_requires_approval_state_not_text(pipeline):
    """Approval state cannot be derived from message text: typing it in chat changes nothing."""
    rec = await propose(pipeline)
    reply = await pipeline.handle(thread_id=THREAD, user_id=APPROVER, text=f"{rec.id} 승인했어, 실행해",
                                  channel_id="C1")
    assert reply.route == "approval_text"
    assert pipeline.approvals.store.get(rec.id).status == "PENDING"
    with pytest.raises(ApprovalError):
        await pipeline.execute(rec.id)
    assert pipeline.approvals.store.get(rec.id).execution_id is None


async def test_approved_execution_runs_verify_and_passes(pipeline):
    rec = await propose(pipeline)
    pipeline.decide(rec.id, APPROVER, "approve")
    seen = []

    async def progress(msg):
        seen.append(msg)

    final = await pipeline.execute(rec.id, progress=progress)
    assert final.status == "SUCCEEDED" and final.execution_id.startswith("mock-")
    assert final.verify["result"] == "PASS"
    assert final.verify["alarms"] == {"checkout-api-5xx-rate-high": "OK"}
    assert all(v <= 1.0 for _, v in final.verify["after"][-2:])
    assert any("실행 시작" in m for m in seen)
    events = [r["event"] for r in pipeline.audit.read(thread_id=THREAD)]
    assert events.index("approval_decided") < events.index("execution_started") < events.index("verify_result")
    notes = pipeline.notes.load(THREAD)
    assert notes["actions"][-1]["status"] == "SUCCEEDED" and notes["actions"][-1]["verify"] == "PASS"


async def test_failed_execution_reports_without_verify(pipeline):
    pipeline.executor.fail_documents.add("AWS-RestartEC2Instance")
    rec = await propose(pipeline)
    pipeline.decide(rec.id, APPROVER, "approve")
    final = await pipeline.execute(rec.id)
    assert final.status == "FAILED" and final.verify["result"] == "NOT_RUN"


async def test_tampered_record_with_non_allowlisted_document_is_not_executed(pipeline):
    rec = await propose(pipeline)
    pipeline.decide(rec.id, APPROVER, "approve")
    pipeline.approvals.store.set_fields(rec.id, document="AWS-DeleteStack")  # simulate DB tampering
    with pytest.raises(ApprovalError) as e:
        await pipeline.execute(rec.id)
    assert e.value.code == "not_allowlisted"
    assert pipeline.approvals.store.get(rec.id).status == "FAILED"
    assert pipeline.approvals.store.get(rec.id).execution_id is None


def test_executor_refuses_non_allowlisted_document(pipeline):
    from aiops.executor import NotAllowlisted

    with pytest.raises(NotAllowlisted):
        pipeline.executor.start(document="AWS-RunShellScript", version="$DEFAULT", parameters={},
                                approval_id="apr-x", approved_by=APPROVER)


def test_ssm_client_token_is_stable_uuid():
    import uuid

    from aiops.executor import client_token

    assert client_token("apr-1") == client_token("apr-1") != client_token("apr-2")
    uuid.UUID(client_token("apr-1"))


async def test_concurrent_execute_only_once(pipeline):
    rec = await propose(pipeline)
    pipeline.decide(rec.id, APPROVER, "approve")
    results = await asyncio.gather(pipeline.execute(rec.id), pipeline.execute(rec.id), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    errs = [r for r in results if isinstance(r, ApprovalError)]
    assert len(ok) == 1 and len(errs) == 1 and errs[0].code == "not_approved"
