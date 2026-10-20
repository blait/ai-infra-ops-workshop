"""AIOPS_MODE=aws code paths, verified offline with botocore Stubber.

Stubber short-circuits the HTTP call *after* botocore validated the request against the service
model, so these tests catch wrong parameter names/shapes without touching AWS.
"""

from datetime import date, datetime, timezone

import boto3
import pytest
from botocore.stub import ANY, Stubber

from aiops.config import PROJECT_ROOT
from aiops.executor import SsmExecutor, client_token
from aiops.tools.backends import AwsBackend, load_services_toml

T0 = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 15, 5, 30, tzinfo=timezone.utc)
EXEC_ID = "3f2a6c1e-8b4d-4c2a-9e7f-1a2b3c4d5e6f"


@pytest.fixture
def session():
    return boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="ap-northeast-2")


@pytest.fixture
def services():
    return load_services_toml(PROJECT_ROOT / "config" / "services.example.toml")


@pytest.fixture
def backend(services, session):
    b = AwsBackend(services, region="ap-northeast-2", session=session)
    b.query_timeout_s = 2
    return b


def test_services_example_parses(services):
    svc = services["checkout-api"]
    assert svc.instance_tag == ("Service", "checkout-api")
    assert svc.metrics["5xx_rate"].expression and len(svc.metrics["5xx_rate"].queries) == 2


def test_describe_alarms(backend):
    with Stubber(backend.cw) as st:
        st.add_response("describe_alarms", {"MetricAlarms": [{
            "AlarmName": "checkout-api-5xx-rate-high", "StateValue": "ALARM", "StateReason": "Threshold Crossed",
            "StateUpdatedTimestamp": T1, "MetricName": "HTTPCode_Target_5XX_Count", "Threshold": 2.0}]},
            {"AlarmTypes": ["MetricAlarm", "CompositeAlarm"], "AlarmNamePrefix": "checkout-api-", "StateValue": "ALARM"})
        out = backend.describe_alarms("checkout-api-", "ALARM")
    assert out[0]["service"] == "checkout-api" and out[0]["since"] == "2026-10-15T05:30:00Z"


def test_metric_math_query(backend):
    with Stubber(backend.cw) as st:
        st.add_response("get_metric_data", {"MetricDataResults": [
            {"Id": "e1", "Label": "5xx_rate", "Timestamps": [T0, T1], "Values": [0.2, 12.1], "StatusCode": "Complete"}]},
            {"MetricDataQueries": ANY, "StartTime": T0, "EndTime": T1, "ScanBy": "TimestampAscending"})
        pts = backend.metric_series("checkout-api", "5xx_rate", T0, T1)
    assert pts == [(T0, 0.2), (T1, 12.1)]


def test_logs_insights_queries(backend):
    with Stubber(backend.logs) as st:
        for qid in ("q1", "q2", "q3", "q4"):
            st.add_response("start_query", {"queryId": qid},
                            {"logGroupName": "/prod/checkout-api/app", "startTime": int(T0.timestamp()),
                             "endTime": int(T1.timestamp()), "queryString": ANY, "limit": ANY})
        row = lambda **kv: [{"field": k, "value": v} for k, v in kv.items()]  # noqa: E731
        st.add_response("get_query_results", {"status": "Complete", "results": [
            row(**{"@timestamp": "2026-10-15 04:44:30.000", "@logStream": "i-0a1b2c3d4e5f60001",
                   "@message": "ERROR HikariPool-1 - Connection is not available"})]}, {"queryId": "q1"})
        st.add_response("get_query_results", {"status": "Complete", "results": []}, {"queryId": "q2"})
        st.add_response("get_query_results", {"status": "Complete", "results": [
            row(**{"@logStream": "i-0a1b2c3d4e5f60001", "n": "620"})]}, {"queryId": "q3"})
        st.add_response("get_query_results", {"status": "Complete", "results": [
            row(t="2026-10-15 04:45:00.000", n="57")]}, {"queryId": "q4"})
        res = backend.logs_rows("checkout-api", T0, T1, 200)
    assert res.total == 620 and res.rows[0]["ts"] == "2026-10-15T04:44:30Z"
    assert res.count_by_bin == {"2026-10-15T04:45:00Z": 57}


def test_cloudtrail_lookup_write_events(backend):
    with Stubber(backend.trail) as st:
        st.add_response("lookup_events", {"Events": [{
            "EventId": "e1", "EventName": "CreateDeployment", "EventTime": T0, "EventSource": "codedeploy.amazonaws.com",
            "Username": "gha-run-8812", "ReadOnly": "false", "Resources": [{"ResourceName": "checkout-api"}],
            "CloudTrailEvent": '{"requestParameters": {"applicationName": "checkout-api"}}'}]},
            {"LookupAttributes": [{"AttributeKey": "ReadOnly", "AttributeValue": "false"}], "StartTime": T0,
             "EndTime": T1, "MaxResults": 50})
        out = backend.trail_events(T0, T1)
    assert out[0]["event"] == "CreateDeployment" and out[0]["params"] == {"applicationName": "checkout-api"}


def test_cost_explorer(backend):
    with Stubber(backend.ce) as st:
        st.add_response("get_cost_and_usage", {"ResultsByTime": [{
            "TimePeriod": {"Start": "2026-10-14", "End": "2026-10-15"}, "Estimated": False,
            "Groups": [{"Keys": ["EC2 - Other"], "Metrics": {"UnblendedCost": {"Amount": "213.7", "Unit": "USD"}}}]}]},
            {"TimePeriod": {"Start": "2026-10-14", "End": "2026-10-15"}, "Granularity": "DAILY",
             "Metrics": ["UnblendedCost"], "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]})
        rows = backend.cost_by_service(date(2026, 10, 14), date(2026, 10, 15))
    assert rows == [{"date": "2026-10-14", "service": "EC2 - Other", "amount": 213.7, "estimated": False}]


def test_service_inventory_by_tag(backend):
    with Stubber(backend.ec2) as st:
        st.add_response("describe_instances", {"Reservations": [{"Instances": [{
            "InstanceId": "i-0a1b2c3d4e5f60001", "State": {"Name": "running", "Code": 16},
            "Placement": {"AvailabilityZone": "ap-northeast-2a"}}]}]},
            {"Filters": [{"Name": "tag:Service", "Values": ["checkout-api"]},
                         {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]})
        out = backend.service_instances("checkout-api")
    assert out == [{"id": "i-0a1b2c3d4e5f60001", "state": "running", "az": "ap-northeast-2a"}]


def test_ssm_executor_request_shape(session):
    ex = SsmExecutor(region="ap-northeast-2", allowed_documents=frozenset({"AWS-RestartEC2Instance"}), session=session)
    with Stubber(ex.ssm) as st:
        st.add_response("start_automation_execution", {"AutomationExecutionId": EXEC_ID}, {
            "DocumentName": "AWS-RestartEC2Instance", "DocumentVersion": "$DEFAULT",
            "Parameters": {"InstanceId": ["i-0a1b2c3d4e5f60001"]}, "ClientToken": client_token("apr-1"),
            "Tags": [{"Key": "aiops:approval_id", "Value": "apr-1"}, {"Key": "aiops:approved_by", "Value": "U0APPROVER"}]})
        st.add_response("get_automation_execution", {"AutomationExecution": {
            "AutomationExecutionId": EXEC_ID, "AutomationExecutionStatus": "Success"}},
            {"AutomationExecutionId": EXEC_ID})
        assert ex.start(document="AWS-RestartEC2Instance", version="$DEFAULT",
                        parameters={"InstanceId": ["i-0a1b2c3d4e5f60001"]}, approval_id="apr-1",
                        approved_by="U0APPROVER") == EXEC_ID
        assert ex.status(EXEC_ID)["status"] == "Success"
