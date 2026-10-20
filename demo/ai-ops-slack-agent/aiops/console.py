"""Local operator console adapter. AWS credentials and task tokens stay server-side.

The model can propose changes through Gateway. Only this separately authenticated
AWS operator path can decide a proposal and resume its Step Functions callback.
"""
from __future__ import annotations

import json
import re
import tempfile
import uuid
from pathlib import Path

import boto3
from botocore.config import Config

from .approvals import ApprovalError, ApprovalService
from .audit import AuditLog
from .cloud.stores import DynamoApprovalStore
from .memory import AgentCoreMemory, MemoryActionSink, NotesMemorySync
from .notes import NotesStore

CHANNEL = "streamlit-console"
ROOT = Path(__file__).resolve().parents[1]


def display_text(value) -> str:
    """Presentation-only masking; never change the actual identifiers sent to AWS."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
    text = re.sub(r"arn:aws[a-z-]*:[^\s\"'<>`,\]]+", "[AWS ARN]", text)
    text = re.sub(r"(?<!\d)\d{12}(?!\d)", "[계정]", text)
    text = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[AWS KEY]", text)
    text = re.sub(r"(?<![A-Za-z0-9])i-[0-9a-f]{8,17}(?![0-9a-f])", lambda m: "i-••••" + m[0][-4:], text)
    text = re.sub(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b", "[내부 IP]", text)
    return text


def new_session(mode="scenario"):
    return f"streamlit-{mode}-{uuid.uuid4().hex}"


def consume_stream(response, on_progress):
    result = None
    try:
        for line in response.iter_lines():
            line = line.decode() if isinstance(line, bytes) else line
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            if event.get("type") == "progress":
                on_progress(event.get("message", ""))
            elif event.get("type") == "result":
                result = event
    finally:
        response.close()
    if result is None or result.get("status") != "success":
        raise RuntimeError((result or {}).get("error", "Runtime이 결과 없이 응답을 종료했습니다."))
    return result


class AwsConsole:
    def __init__(self, region="us-east-1", stack="AiopsAgentStack", session=None):
        self.region, self.stack = region, stack
        self.session = session or boto3.Session(region_name=region)
        data = self.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
        self.outputs = {o["OutputKey"]: o["OutputValue"] for o in data["Outputs"]}
        self.stack_status = data["StackStatus"]
        self.store = DynamoApprovalStore(self.outputs["TableName"], client=self.client("dynamodb"))
        self.memory = AgentCoreMemory(self.outputs["MemoryId"], region, client=self.client("bedrock-agentcore"))

    def client(self, service, **kwargs):
        return self.session.client(service, region_name=self.region, **kwargs)

    def identity(self):
        return self.client("sts").get_caller_identity()["Arn"]

    def status(self):
        control = self.client("bedrock-agentcore-control")
        runtime = control.get_agent_runtime(agentRuntimeId=self.outputs["RuntimeId"])
        gateway = control.get_gateway(gatewayIdentifier=self.outputs["GatewayId"])
        memory = control.get_memory(memoryId=self.outputs["MemoryId"])["memory"]
        targets = control.list_gateway_targets(gatewayIdentifier=self.outputs["GatewayId"]).get("items", [])
        config = gateway.get("policyEngineConfiguration", {})
        env = runtime.get("environmentVariables", {})
        toolkit_ready = any(t["name"] == "awstoolkit" and t["status"] == "READY" for t in targets)
        checks = {
            "Runtime": runtime.get("status") == "READY",
            "Gateway": gateway.get("status") == "READY" and gateway.get("authorizerType") == "AWS_IAM",
            "Policy": config.get("mode") == "ENFORCE" and self.outputs["PolicyEngineId"] in config.get("arn", ""),
            "Memory": memory.get("status") == "ACTIVE" and env.get("AIOPS_MEMORY_ID") == self.outputs["MemoryId"],
            "AWS Toolkit": toolkit_ready and "awstoolkit:aws_mcp_read" in env.get("AIOPS_GATEWAY_TARGETS", ""),
        }
        return {"ready": all(checks.values()), "checks": checks, "runtime_version": runtime.get("agentRuntimeVersion"),
                "region": self.region, "stack_status": self.stack_status, "model": env.get("AIOPS_MODEL"),
                "retention_days": memory.get("eventExpiryDuration")}

    def invoke(self, prompt, session_id, mode, on_progress):
        if mode not in {"scenario", "aws_live"} or not session_id.startswith(f"streamlit-{mode}-"):
            raise ValueError("데이터 모드와 세션이 일치하지 않습니다.")
        response = self.client("bedrock-agentcore", config=Config(read_timeout=900, retries={"max_attempts": 0})).invoke_agent_runtime(
            agentRuntimeArn=self.outputs["RuntimeArn"], runtimeSessionId=session_id,
            payload=json.dumps({"prompt": prompt, "user_id": self.identity(), "channel_id": CHANNEL,
                                "mode": mode, "stream": True}).encode(),
            contentType="application/json", accept="text/event-stream")
        return consume_stream(response["response"], on_progress)

    def notes(self, session_id):
        with tempfile.TemporaryDirectory(prefix="aiops-console-notes-") as directory:
            notes = NotesStore(Path(directory))
            restored = NotesMemorySync(self.memory, notes).hydrate(session_id)
            data = notes.load(session_id)
            evidence = [notes.get_evidence(session_id, e) for e in sorted(notes.evidence_ids(session_id))]
            return {"restored": restored, "notes": data, "markdown": notes.render_markdown(data), "evidence": evidence}

    def _record(self, approval_id, session_id):
        record = self.store.get(approval_id)
        if record is None:
            raise ApprovalError("not_found", "승인 요청을 찾을 수 없습니다.")
        if record.thread_id != session_id or record.channel_id != CHANNEL:
            raise ApprovalError("wrong_context", "현재 Streamlit 세션의 승인 요청이 아닙니다.")
        return record

    def approval(self, approval_id, session_id):
        record = self._record(approval_id, session_id)
        item = self.store.get_item(approval_id) or {}
        view = record.to_dict()
        if item.get("sfn_execution_arn"):
            execution = self.client("stepfunctions").describe_execution(executionArn=item["sfn_execution_arn"])
            view["flow_status"] = execution["status"]
        return view  # Never expose raw item, callback token, or credentials to the browser.

    def decide(self, approval_id, session_id, decision):
        record = self._record(approval_id, session_id)
        actor = self.identity()  # Derive actor from AWS, never from a browser field or model output.
        service = ApprovalService(
            store=self.store, runbooks=None, backend=None, executor=None,
            audit=AuditLog(ROOT / "var" / "console-audit.jsonl"),
            notes=MemoryActionSink(self.memory), approvers=frozenset({actor}))
        service.decide(record.id, actor, decision, channel_id=CHANNEL, source="streamlit_aws_operator")
        return self.resume_decision(approval_id, session_id)

    def resume_decision(self, approval_id, session_id):
        record = self._record(approval_id, session_id)
        actor = self.identity()
        if record.status not in {"APPROVED", "REJECTED"} or record.decided_by != actor:
            raise ApprovalError("not_decider", "본인이 처리한 승인·거절 결정만 전달할 수 있습니다.")
        item = self.store.get_item(approval_id) or {}
        if item.get("task_token"):
            try:
                self.client("stepfunctions").send_task_success(
                    taskToken=item["task_token"],
                    output=json.dumps({"decision": "approve" if record.status == "APPROVED" else "reject",
                                       "decided_by": actor}))
            except Exception as exc:
                raise RuntimeError("결정은 저장됐지만 실행 흐름에 전달하지 못했습니다. 상태를 새로고침한 뒤 "
                                   "'결정 전달 재시도'로 복구하세요. " + display_text(str(exc))) from exc
        # If the token is not registered yet, the existing registration Lambda sends this decision.
        return self.approval(approval_id, session_id)

    def reset_fixture(self):
        self.client("dynamodb").delete_item(TableName=self.outputs["TableName"],
                                            Key={"pk": {"S": "MOCKSTATE#default"}})
