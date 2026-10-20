import json

import boto3
import pytest
from botocore.stub import ANY, Stubber

from aiops.approvals import LockConflict
from aiops.cloud.stores import DynamoApprovalStore, DynamoMockState, to_ddb

T = "aiops-approvals"
NOW = 1_800_000_000


@pytest.fixture
def ddb():
    c = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    with Stubber(c) as st:
        yield c, st
        st.assert_no_pending_responses()


def rec(**kw):
    base = {"id": "apr-1", "thread_id": "t1", "channel_id": "C1", "card_ts": None, "requested_by": "U1",
            "runbook_id": "restart-ec2-instance", "document": "AWS-RestartEC2Instance", "document_version": "$DEFAULT",
            "params_json": json.dumps({"InstanceId": "i-0a1b2c3d4e5f60001"}), "target_hash": "h1", "reason": "r" * 12,
            "evidence_json": "[]", "status": "PENDING", "created_at": "2026-10-15T05:30:00+00:00",
            "expires_at": "2026-10-15T06:00:00+00:00"}
    base.update(kw)
    return base


def store(c):
    return DynamoApprovalStore(T, client=c, clock=lambda: NOW)


def test_insert_is_one_transaction_with_three_conditional_puts(ddb):
    c, st = ddb
    captured = {}

    def check(params):
        captured.update(params)

    st.add_response("transact_write_items", {}, {"TransactItems": ANY})
    c.meta.events.register("provide-client-params.dynamodb.TransactWriteItems", lambda params, **_: check(params))
    store(c).insert(rec())
    items = captured["TransactItems"]
    assert len(items) == 3 and all("Put" in i for i in items)
    apr, lt, lg = (i["Put"] for i in items)
    assert apr["Item"]["pk"] == {"S": "APR#apr-1"} and apr["ConditionExpression"] == "attribute_not_exists(pk)"
    assert lt["Item"]["pk"] == {"S": "LOCK#THREAD#t1"} and lg["Item"]["pk"] == {"S": "LOCK#TARGET#h1"}
    for lock in (lt, lg):
        assert "lock_expires < :now" in lock["ConditionExpression"]
        assert lock["ExpressionAttributeValues"] == {":now": {"N": str(NOW)}}
        assert lock["Item"]["approval_id"] == {"S": "apr-1"}
    assert "card_ts" not in apr["Item"]  # None values are not written


def test_insert_conflict_raises_lock_conflict(ddb):
    c, st = ddb
    st.add_client_error("transact_write_items", "TransactionCanceledException")
    # stale-lock check: thread lock held by an ACTIVE approval -> not stale; target lock missing
    st.add_response("get_item", {"Item": to_ddb({"pk": "LOCK#THREAD#t1", "approval_id": "apr-0",
                                                 "lock_expires": NOW + 100})},
                    {"TableName": T, "Key": {"pk": {"S": "LOCK#THREAD#t1"}}, "ConsistentRead": True})
    st.add_response("get_item", {"Item": to_ddb({"pk": "APR#apr-0", **rec(id="apr-0")})},
                    {"TableName": T, "Key": {"pk": {"S": "APR#apr-0"}}, "ConsistentRead": True})
    st.add_response("get_item", {}, {"TableName": T, "Key": {"pk": {"S": "LOCK#TARGET#h1"}}, "ConsistentRead": True})
    with pytest.raises(LockConflict):
        store(c).insert(rec())


def test_transition_condition_and_false_on_conflict(ddb):
    c, st = ddb
    st.add_response("update_item", {}, {
        "TableName": T, "Key": {"pk": {"S": "APR#apr-1"}}, "UpdateExpression": "SET #f0 = :f0, #f1 = :f1",
        "ConditionExpression": "attribute_exists(pk) AND #s IN (:s0)",
        "ExpressionAttributeNames": {"#s": "status", "#f0": "status", "#f1": "decided_by"},
        "ExpressionAttributeValues": {":s0": {"S": "PENDING"}, ":f0": {"S": "APPROVED"}, ":f1": {"S": "U9"}}})
    st.add_client_error("update_item", "ConditionalCheckFailedException")
    s = store(c)
    assert s.transition("apr-1", ("PENDING",), "APPROVED", decided_by="U9") is True
    assert s.transition("apr-1", ("PENDING",), "APPROVED", decided_by="U8") is False


def test_terminal_transition_releases_locks(ddb):
    c, st = ddb
    st.add_response("update_item", {}, {"TableName": T, "Key": {"pk": {"S": "APR#apr-1"}}, "UpdateExpression": ANY,
                                        "ConditionExpression": "attribute_exists(pk) AND #s IN (:s0, :s1)",
                                        "ExpressionAttributeNames": ANY, "ExpressionAttributeValues": ANY})
    st.add_response("get_item", {"Item": to_ddb({"pk": "APR#apr-1", **rec()})},
                    {"TableName": T, "Key": {"pk": {"S": "APR#apr-1"}}, "ConsistentRead": True})
    for pk in ("LOCK#THREAD#t1", "LOCK#TARGET#h1"):
        st.add_response("delete_item", {}, {"TableName": T, "Key": {"pk": {"S": pk}},
                                            "ConditionExpression": "approval_id = :a",
                                            "ExpressionAttributeValues": {":a": {"S": "apr-1"}}})
    assert store(c).transition("apr-1", ("PENDING", "APPROVED"), "EXPIRED") is True


def test_get_parses_item(ddb):
    c, st = ddb
    st.add_response("get_item", {"Item": to_ddb({"pk": "APR#apr-1", **rec(verify_json='{"result": "PASS"}')})},
                    {"TableName": T, "Key": {"pk": {"S": "APR#apr-1"}}, "ConsistentRead": True})
    a = store(c).get("apr-1")
    assert a.params == {"InstanceId": "i-0a1b2c3d4e5f60001"} and a.verify == {"result": "PASS"} and a.card_ts is None
    assert store(c).get("") is None


def test_claim_event(ddb):
    c, st = ddb
    exp = {"TableName": T, "Item": ANY, "ConditionExpression": "attribute_not_exists(pk)"}
    st.add_response("put_item", {}, exp)
    st.add_client_error("put_item", "ConditionalCheckFailedException", expected_params=exp)
    s = store(c)
    assert s.claim_event("Ev1") is True and s.claim_event("Ev1") is False


def test_mock_state_load_default_and_versioned_update(ddb):
    c, st = ddb
    key = {"TableName": T, "Key": {"pk": {"S": "MOCKSTATE#default"}}, "ConsistentRead": True}
    st.add_response("get_item", {}, key)
    st.add_response("get_item", {}, key)
    st.add_response("put_item", {}, {"TableName": T, "Item": ANY, "ConditionExpression": "attribute_not_exists(pk)"})
    st.add_response("get_item", {"Item": to_ddb({"pk": "MOCKSTATE#default", "state_json": '{"advance_minutes": 15}',
                                                 "version": 1})}, key)
    st.add_response("put_item", {}, {"TableName": T, "Item": ANY, "ConditionExpression": "version = :v",
                                     "ExpressionAttributeValues": {":v": {"N": "1"}}})
    ms = DynamoMockState(T, client=c)
    assert ms.load() == {"advance_minutes": 0, "restarts": [], "executions": {}}
    assert ms.update(lambda s: s.setdefault("advance_minutes", 0)) == 0
    assert ms.update(lambda s: s["advance_minutes"]) == 15
