"""AiopsAgentStack: the module 3 demo in the production shape recommended by research/03.

  Slack ── API GW (HTTP) ── ingress λ (signature, 3 s ack) ── SQS FIFO (group = thread) ── worker λ
                     └── interactivity λ (approval buttons) ── DynamoDB CAS ── SendTaskSuccess
  worker λ ── InvokeAgentRuntime (session = thread) ── AgentCore Runtime (Claude Agent SDK container)
  Runtime ── SigV4 ── AgentCore Gateway (IAM inbound) ── Policy engine (Cedar, ENFORCE)
          ├ readtools       λ  read-only tools + runbook_list/get       (role: read-only + explicit Deny)
          ├ changeproposal  λ  runbook_propose -> DynamoDB PENDING + start Step Functions
          ├ forbidden       λ  decoy mutating tools, Cedar forbid (never reached)
          └ awsdocs         MCP server  AWS Knowledge MCP (public, no outbound auth): only search/read
                            documentation are permitted by Cedar (grounding for manual-fix drafts)
  Runtime ── AgentCore Memory (short-term events per thread: notes, evidence, actions, turns)
  Step Functions (Standard): waitForTaskToken (heartbeat 30 min) -> executor λ (own role: SSM start on
  the allowlisted document + PassRole) -> poll -> verify λ (status + metric + alarm + instance state)
  -> notifier λ. SSM Automation (AWS-RestartEC2Instance) runs as the automation role, which can only
  stop/start instances tagged Service=checkout-api AND aiops:demo-target=true (the one t4g.nano).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_integrations as apigw_int,
    aws_bedrockagentcore as agentcore,
    aws_dynamodb as ddb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as les,
    aws_logs as logs,
    aws_secretsmanager as sm,
    aws_sqs as sqs,
    aws_ssm as ssm,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
)
from constructs import Construct

from .bundle import DEMO_DIR, build_lambda_bundle

sys.path.insert(0, str(DEMO_DIR))
from aiops.tools.backends import MockBackend, MockState  # noqa: E402
from aiops.tools.core import metric_hint, tool_specs  # noqa: E402
from aiops.aws_toolkit import TOOLKIT_READ_ACTIONS, TOOLKIT_SPEC, TOOLKIT_TOOLS  # noqa: E402

READ_TOOLS = ["cloudwatch_alarm_status", "cloudwatch_metric_summary", "logs_error_summary",
              "cloudtrail_recent_changes", "cost_daily_delta", "runbook_list", "runbook_get"]
PROPOSE_TOOLS = ["runbook_propose"]
DECOY_TOOLS = {
    "security_group_open_to_world": ("보안그룹 인바운드를 0.0.0.0/0으로 연다 (금지 도구: Cedar forbid 시연용, 실행 코드 없음)",
                                     {"type": "object", "properties": {"group_id": {"type": "string"},
                                                                       "port": {"type": "integer"}},
                                      "required": ["group_id"]}),
    "ssm_run_any_automation": ("임의 SSM Automation 문서를 실행한다 (금지 도구: Cedar forbid 시연용, 실행 코드 없음)",
                               {"type": "object", "properties": {"document": {"type": "string"},
                                                                 "parameters": {"type": "object"}},
                                "required": ["document"]}),
}
READ_TARGET, PROPOSE_TARGET, DECOY_TARGET, DOCS_TARGET = "readtools", "changeproposal", "forbidden", "awsdocs"
DOCS_MCP_ENDPOINT = "https://knowledge-mcp.global.api.aws"
DOCS_TOOLS = ["aws___search_documentation", "aws___read_documentation"]  # the server's other tools stay default-deny
ALLOWED_DOCUMENT = "AWS-RestartEC2Instance"
DEMO_SERVICE_TAG = "checkout-api"
MUTATING_DENY = [
    "ssm:StartAutomationExecution", "ssm:SendCommand", "ssm:StartSession", "ec2:StopInstances",
    "ec2:StartInstances", "ec2:RebootInstances", "ec2:TerminateInstances", "ec2:AuthorizeSecurityGroupIngress",
    "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress", "ec2:ModifyInstanceAttribute", "iam:*",
    "codedeploy:CreateDeployment", "codedeploy:StopDeployment", "cloudtrail:StopLogging", "cloudtrail:DeleteTrail",
    "rds:Delete*", "rds:Modify*",
]


def _schema_def(js: dict) -> agentcore.SchemaDefinition:
    t = {"object": agentcore.SchemaDefinitionType.OBJECT, "string": agentcore.SchemaDefinitionType.STRING,
         "integer": agentcore.SchemaDefinitionType.INTEGER, "number": agentcore.SchemaDefinitionType.NUMBER,
         "boolean": agentcore.SchemaDefinitionType.BOOLEAN, "array": agentcore.SchemaDefinitionType.ARRAY}[js["type"]]
    desc = js.get("description")
    if js.get("enum"):
        desc = (desc + " " if desc else "") + f"(허용 값: {', '.join(js['enum'])})"
    props = {k: _schema_def(v) for k, v in (js.get("properties") or {}).items()} or None
    return agentcore.SchemaDefinition(type=t, description=desc, properties=props,
                                      required=js.get("required") or None,
                                      items=_schema_def(js["items"]) if js.get("items") else None)


def _tool_schema(defs: dict[str, tuple[str, dict]]) -> agentcore.ToolSchema:
    return agentcore.ToolSchema.from_inline([
        agentcore.ToolDefinition(name=name, description=desc[:200], input_schema=_schema_def(schema))
        for name, (desc, schema) in defs.items()])


class AiopsAgentStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)
        ctx = self.node.try_get_context
        model_id = ctx("aiops:model") or "us.anthropic.claude-sonnet-5-5"
        approvers = ctx("aiops:approvers") or "REPLACE_WITH_SLACK_USER_IDS"
        slack_dry_run = str(ctx("aiops:slackDryRun") or "true").lower()
        demo_az = ctx("aiops:demoTargetAz") or "us-east-1a"
        acct, region = self.account, self.region
        Tags.of(self).add("project", "aiops-workshop-module3")

        code = lambda_.Code.from_asset(str(build_lambda_bundle()))

        # ------------------------------------------------------------------ data stores
        table = ddb.Table(
            self, "Approvals", partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST, time_to_live_attribute="ttl",
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            encryption=ddb.TableEncryption.AWS_MANAGED, removal_policy=RemovalPolicy.DESTROY)

        slack_secret = sm.Secret(
            self, "SlackSecret", description="Slack bot token + signing secret (placeholders; owner fills in)",
            generate_secret_string=sm.SecretStringGenerator(
                secret_string_template=json.dumps({"bot_token": "REPLACE_WITH_xoxb_BOT_TOKEN"}),
                generate_string_key="signing_secret", exclude_punctuation=True, password_length=40),
            removal_policy=RemovalPolicy.DESTROY)

        approvers_param = ssm.StringParameter(
            self, "Approvers", parameter_name="/aiops-agent/approvers", string_value=approvers,
            description="Comma-separated Slack user ids allowed to approve runbook executions")

        dlq = sqs.Queue(self, "WorkDlq", fifo=True, retention_period=Duration.days(4),
                        encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True)
        queue = sqs.Queue(self, "WorkQueue", fifo=True, content_based_deduplication=False,
                          visibility_timeout=Duration.seconds(960), retention_period=Duration.hours(4),
                          encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True,
                          dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=1))

        memory = agentcore.Memory(self, "ThreadMemory", memory_name="aiops_thread_notes",
                                  description="Per-Slack-thread structured notes, evidence and approval actions",
                                  expiration_duration=Duration.days(30))

        # ------------------------------------------------------------------ demo target (the only thing SSM may touch)
        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)
        target_sg = ec2.SecurityGroup(self, "DemoTargetSg", vpc=vpc, allow_all_outbound=False,
                                      description="aiops demo target: no inbound, no outbound")
        target_role = iam.Role(self, "DemoTargetRole", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
                               description="aiops demo target instance role (no permissions)")
        demo = ec2.Instance(
            self, "DemoTarget", vpc=vpc, vpc_subnets=ec2.SubnetSelection(availability_zones=[demo_az],
                                                                         subnet_type=ec2.SubnetType.PUBLIC),
            instance_type=ec2.InstanceType("t4g.nano"), role=target_role, security_group=target_sg,
            machine_image=ec2.MachineImage.from_ssm_parameter(
                "/aws/service/ami-amazon-linux-latest/al2023-ami-minimal-kernel-default-arm64"),
            associate_public_ip_address=False, require_imdsv2=True,
            block_devices=[ec2.BlockDevice(device_name="/dev/xvda", volume=ec2.BlockDeviceVolume.ebs(
                2, encrypted=True, volume_type=ec2.EbsDeviceVolumeType.GP3, delete_on_termination=True))])
        Tags.of(demo).add("Name", "aiops-demo-target")
        Tags.of(demo).add("Service", DEMO_SERVICE_TAG)
        Tags.of(demo).add("aiops:demo-target", "true")
        instance_map = f"i-0a1b2c3d4e5f60001={demo.instance_id}"

        automation_role = iam.Role(
            self, "AutomationRole", description="Assumed by SSM Automation for AWS-RestartEC2Instance only",
            assumed_by=iam.ServicePrincipal("ssm.amazonaws.com", conditions={
                "StringEquals": {"aws:SourceAccount": acct},
                "ArnLike": {"aws:SourceArn": f"arn:aws:ssm:{region}:{acct}:automation-execution/*"}}))
        automation_role.add_to_policy(iam.PolicyStatement(
            sid="StopStartOnlyTheTaggedDemoTarget", actions=["ec2:StopInstances", "ec2:StartInstances"],
            resources=[f"arn:aws:ec2:{region}:{acct}:instance/*"],
            conditions={"StringEquals": {"aws:ResourceTag/Service": DEMO_SERVICE_TAG,
                                         "aws:ResourceTag/aiops:demo-target": "true"}}))
        automation_role.add_to_policy(iam.PolicyStatement(
            sid="DescribeForChangeInstanceState", actions=["ec2:DescribeInstances", "ec2:DescribeInstanceStatus"],
            resources=["*"]))

        # ------------------------------------------------------------------ Lambda helpers
        def log_group(name: str) -> logs.LogGroup:
            return logs.LogGroup(self, f"{name}Logs", log_group_name=f"/aws/lambda/aiops-{name.lower()}",
                                 retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY)

        base_env = {"AIOPS_MODE": "mock", "AIOPS_TABLE": table.table_name, "AIOPS_DATA_DIR": "/tmp/aiops",
                    "AIOPS_MOCK_INSTANCE_MAP": instance_map, "AIOPS_SLACK_DRY_RUN": slack_dry_run,
                    "AIOPS_DISPLAY_TZ": "Asia/Seoul", "AIOPS_WORKFLOW_LLM": "0"}

        def fn(name: str, handler: str, *, timeout: int, memory_mb: int = 512, env: dict | None = None,
               role: iam.Role | None = None) -> lambda_.Function:
            role = role or iam.Role(self, f"{name}Role", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                                    description=f"aiops {name} Lambda")
            lg = log_group(name)
            role.add_to_policy(iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                                   resources=[lg.log_group_arn, f"{lg.log_group_arn}:*"]))
            f = lambda_.Function(self, name, function_name=f"aiops-{name.lower()}", runtime=lambda_.Runtime.PYTHON_3_12,
                                 architecture=lambda_.Architecture.ARM_64, handler=f"aiops.cloud.handlers.{handler}",
                                 code=code, timeout=Duration.seconds(timeout), memory_size=memory_mb, role=role,
                                 environment={**base_env, **(env or {})}, log_group=lg,
                                 tracing=lambda_.Tracing.ACTIVE)
            role.add_to_policy(iam.PolicyStatement(actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                                                   resources=["*"]))
            return f

        def ddb_keys(role: iam.Role, actions: list[str], prefixes: list[str]) -> None:
            role.add_to_policy(iam.PolicyStatement(
                actions=[f"dynamodb:{a}" for a in actions], resources=[table.table_arn],
                conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{p}*" for p in prefixes]}}))

        def deny_mutations(role: iam.Role, extra: list[str] | None = None) -> None:
            role.add_to_policy(iam.PolicyStatement(sid="DenyChangesEvenIfBroaderPolicyIsAttached",
                                                   effect=iam.Effect.DENY, actions=MUTATING_DENY + (extra or []),
                                                   resources=["*"]))

        def memory_write(role: iam.Role) -> None:
            role.add_to_policy(iam.PolicyStatement(actions=["bedrock-agentcore:CreateEvent"],
                                                   resources=[memory.memory_arn]))

        read_only_aws = iam.PolicyStatement(sid="ReadOnlyToolsForAwsMode", actions=[
            "cloudwatch:DescribeAlarms", "cloudwatch:GetMetricData", "logs:StartQuery", "logs:GetQueryResults",
            "logs:StopQuery", "cloudtrail:LookupEvents", "ce:GetCostAndUsage", "ec2:DescribeInstances"],
            resources=["*"])

        # ------------------------------------------------------------------ Gateway tool targets
        gw_read = fn("GatewayRead", "gateway_read", timeout=60)
        gw_read.role.add_to_policy(read_only_aws)
        ddb_keys(gw_read.role, ["GetItem"], ["MOCKSTATE#"])
        deny_mutations(gw_read.role, ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                                      "states:*", "secretsmanager:*"])

        gw_toolkit = fn("GatewayToolkit", "gateway_toolkit", timeout=120)
        gw_toolkit.role.add_to_policy(iam.PolicyStatement(
            sid="ReadOnlyViaManagedAwsMcp", actions=TOOLKIT_READ_ACTIONS, resources=["*"],
            conditions={"Bool": {"aws:ViaAWSMCPService": "true"}}))
        gw_toolkit.role.add_to_policy(iam.PolicyStatement(
            sid="DenyEverythingElseViaMcp", effect=iam.Effect.DENY,
            not_actions=TOOLKIT_READ_ACTIONS, resources=["*"],
            conditions={"Bool": {"aws:ViaAWSMCPService": "true"}}))
        deny_mutations(gw_toolkit.role, ["secretsmanager:*", "ssm:GetParameter*", "dynamodb:*", "states:*"])

        gw_propose = fn("GatewayPropose", "gateway_propose", timeout=60)
        gw_propose.role.add_to_policy(iam.PolicyStatement(actions=["ec2:DescribeInstances"], resources=["*"]))
        ddb_keys(gw_propose.role, ["GetItem", "PutItem", "UpdateItem", "DeleteItem", "ConditionCheckItem"],
                 ["APR#", "LOCK#", "MOCKSTATE#"])
        memory_write(gw_propose.role)
        deny_mutations(gw_propose.role, ["secretsmanager:*", "states:SendTask*"])

        decoy_role = iam.Role(self, "GatewayDecoyRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                              description="Decoy target: denies every AWS action")
        gw_decoy = fn("GatewayDecoy", "gateway_decoy", timeout=10, memory_mb=128, role=decoy_role)
        decoy_role.add_to_policy(iam.PolicyStatement(effect=iam.Effect.DENY, not_actions=[
            "logs:CreateLogStream", "logs:PutLogEvents", "xray:PutTraceSegments", "xray:PutTelemetryRecords"],
            resources=["*"]))

        services = MockBackend(DEMO_DIR / "fixtures" / "checkout-incident", MockState(Path("/nonexistent"))).services()
        specs = tool_specs(sorted(services), metric_hint(services))
        propose_specs = {"runbook_propose": (specs["runbook_propose"][0], {
            **specs["runbook_propose"][1],
            "properties": {**specs["runbook_propose"][1]["properties"], "caller": {
                "type": "object", "description": "trusted request context, filled by the agent runtime code",
                "properties": {"thread_id": {"type": "string"}, "user_id": {"type": "string"},
                               "channel_id": {"type": "string"},
                               "known_evidence_ids": {"type": "array", "items": {"type": "string"}}}}}})}

        policy_engine = agentcore.PolicyEngine(self, "ToolPolicyEngine", policy_engine_name="aiops_tool_policy",
                                               description="Only read-only tools + runbook_propose; everything else denied")
        gateway = agentcore.Gateway(
            self, "ToolGateway", gateway_name="aiops-ops-tools",
            description="AI-Ops agent tools (read-only + runbook proposal). Cedar policy ENFORCE.",
            authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),
            policy_engine_configuration=agentcore.GatewayPolicyEngineConfig(
                policy_engine=policy_engine, mode=agentcore.PolicyEngineMode.ENFORCE))
        t_read = gateway.add_lambda_target("ReadTarget", gateway_target_name=READ_TARGET, lambda_function=gw_read,
                                           tool_schema=_tool_schema({k: specs[k] for k in READ_TOOLS}),
                                           description="Read-only CloudWatch/Logs/CloudTrail/Cost tools + runbook list/get")
        t_prop = gateway.add_lambda_target("ProposeTarget", gateway_target_name=PROPOSE_TARGET,
                                           lambda_function=gw_propose, tool_schema=_tool_schema(propose_specs),
                                           description="runbook_propose: PENDING approval record only")
        t_decoy = gateway.add_lambda_target("DecoyTarget", gateway_target_name=DECOY_TARGET, lambda_function=gw_decoy,
                                            tool_schema=_tool_schema(DECOY_TOOLS),
                                            description="Mutating tools that the policy forbids (demo of Cedar forbid)")
        # Public AWS Knowledge MCP Server as an MCP-server target. Empty credential provider list = no outbound
        # auth (the server needs none). Gateway syncs its tools/list at create/update; Cedar permits only 2 tools.
        t_docs = gateway.add_mcp_server_target("DocsTarget", gateway_target_name=DOCS_TARGET,
                                               endpoint=DOCS_MCP_ENDPOINT, credential_provider_configurations=[],
                                               description="AWS Knowledge MCP (read-only docs) for grounded manual-fix drafts")
        t_toolkit = gateway.add_lambda_target(
            "ToolkitTarget", gateway_target_name="awstoolkit", lambda_function=gw_toolkit,
            tool_schema=_tool_schema(TOOLKIT_SPEC),
            description="Agent Toolkit for AWS managed MCP, downstream IAM read-only")

        # ------------------------------------------------------------------ AgentCore Runtime (the brain)
        gateway_targets = ";".join([f"{READ_TARGET}:{'|'.join(READ_TOOLS)}", f"{PROPOSE_TARGET}:{'|'.join(PROPOSE_TOOLS)}",
                                   f"awstoolkit:{'|'.join(TOOLKIT_TOOLS)}"])
        runtime_role = iam.Role(self, "RuntimeRole", role_name=f"aiops-agent-runtime-{region}",
                                assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", conditions={
                                    "StringEquals": {"aws:SourceAccount": acct},
                                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{acct}:*"}}),
                                description="AgentCore Runtime: Bedrock invoke + Gateway invoke + Memory only")
        runtime = agentcore.Runtime(
            self, "AgentRuntime", runtime_name="aiops_ops_agent", execution_role=runtime_role,
            description="Claude Agent SDK ops agent (router, workflows, investigator + scouts, reviewer, gates)",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_asset(
                str(DEMO_DIR), file="Dockerfile.agentcore", platform=ecr_assets.Platform.LINUX_ARM64,
                exclude=["infra", ".venv", ".venv-infra", "var", "tests", "evals", ".pytest_cache", "**/__pycache__"]),
            environment_variables={
                "AIOPS_MODE": "mock", "AIOPS_MODEL": model_id, "AIOPS_EVALUATOR_MODEL": model_id,
                "ANTHROPIC_MODEL": model_id, "ANTHROPIC_DEFAULT_HAIKU_MODEL": model_id,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model_id, "CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": region,
                "AIOPS_GATEWAY_URL": gateway.gateway_url, "AIOPS_GATEWAY_TARGETS": gateway_targets,
                "AIOPS_DOCS_GATEWAY_TARGET": DOCS_TARGET, "AIOPS_MANUAL_FIX_DRAFT": "1",
                "AIOPS_MEMORY_ID": memory.memory_id, "AIOPS_MOCK_INSTANCE_MAP": instance_map,
                "AIOPS_AUDIT_STDOUT": "1", "AIOPS_WORKFLOW_LLM": "1", "AIOPS_MAX_BUDGET_USD": "1.50",
                "AIOPS_DATA_DIR": "/tmp/aiops", "HOME": "/tmp/aiops-home"},
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.minutes(15), max_lifetime=Duration.hours(8)),
            tracing_enabled=True)
        runtime.node.add_dependency(t_read, t_prop, t_docs, t_toolkit)
        runtime_role.add_to_policy(iam.PolicyStatement(
            sid="BedrockPinnedModelOnly", actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[f"arn:aws:bedrock:{region}:{acct}:inference-profile/{model_id}",
                       *[f"arn:aws:bedrock:{r}::foundation-model/{model_id.split('.', 1)[1]}"
                         for r in ("us-east-1", "us-east-2", "us-west-2")]]))
        runtime_role.add_to_policy(iam.PolicyStatement(sid="GatewayInvokeThisGatewayOnly",
                                                       actions=["bedrock-agentcore:InvokeGateway"],
                                                       resources=[gateway.gateway_arn]))
        runtime_role.add_to_policy(iam.PolicyStatement(
            sid="ThreadMemoryShortTerm", actions=["bedrock-agentcore:CreateEvent", "bedrock-agentcore:ListEvents",
                                                  "bedrock-agentcore:GetEvent"], resources=[memory.memory_arn]))
        deny_mutations(runtime_role, ["dynamodb:*", "states:*", "secretsmanager:GetSecretValue", "ssm:GetParameter*",
                                      "lambda:InvokeFunction", "sqs:*"])

        # ------------------------------------------------------------------ Cedar policies (default deny)
        gw_arn = gateway.gateway_arn
        runtime_principal = f'AgentCore::IamEntity::"arn:aws:sts::{acct}:assumed-role/{runtime_role.role_name}"'
        read_actions = ", ".join(f'AgentCore::Action::"{READ_TARGET}___{t}"' for t in READ_TOOLS)
        p_read = policy_engine.add_policy("PermitReadOnlyTools", policy_name="permit_read_only_tools",
            description="Read-only tools for IAM principals of this account (who may call the Gateway is IAM's job)",
            statement=agentcore.PolicyStatement.from_cedar(
                f'permit(principal is AgentCore::IamEntity, action in [{read_actions}], '
                f'resource == AgentCore::Gateway::"{gw_arn}") when {{ principal.id like "*:{acct}:*" }};'),
            validation_mode=agentcore.PolicyValidationMode.FAIL_ON_ANY_FINDINGS)
        p_prop = policy_engine.add_policy("PermitProposeFromRuntimeOnly", policy_name="permit_propose_runtime_only",
            description="runbook_propose only from the agent runtime role, only for allowlisted runbook ids",
            statement=agentcore.PolicyStatement.from_cedar(
                f'permit(principal == {runtime_principal}, '
                f'action == AgentCore::Action::"{PROPOSE_TARGET}___runbook_propose", '
                f'resource == AgentCore::Gateway::"{gw_arn}") '
                f'when {{ context.input.runbook_id == "restart-ec2-instance" }};'),
            validation_mode=agentcore.PolicyValidationMode.FAIL_ON_ANY_FINDINGS)
        decoy_actions = ", ".join(f'AgentCore::Action::"{DECOY_TARGET}___{t}"' for t in DECOY_TOOLS)
        p_forbid = policy_engine.add_policy("ForbidMutatingTools", policy_name="forbid_mutating_tools",
            description="Mutating tools are forbidden for everyone (forbid wins over any future permit)",
            statement=agentcore.PolicyStatement.from_cedar(
                f'forbid(principal, action in [{decoy_actions}], resource == AgentCore::Gateway::"{gw_arn}");'),
            validation_mode=agentcore.PolicyValidationMode.IGNORE_ALL_FINDINGS)
        docs_actions = ", ".join(f'AgentCore::Action::"{DOCS_TARGET}___{t}"' for t in DOCS_TOOLS)
        p_docs = policy_engine.add_policy("PermitAwsDocsRead", policy_name="permit_aws_docs_read",
            description="AWS Knowledge MCP: search/read documentation only (public, read-only), this account's principals",
            statement=agentcore.PolicyStatement.from_cedar(
                f'permit(principal is AgentCore::IamEntity, action in [{docs_actions}], '
                f'resource == AgentCore::Gateway::"{gw_arn}") when {{ principal.id like "*:{acct}:*" }};'),
            validation_mode=agentcore.PolicyValidationMode.FAIL_ON_ANY_FINDINGS)
        p_toolkit = policy_engine.add_policy(
            "PermitAwsToolkitRead", policy_name="permit_aws_toolkit_read",
            description="Managed AWS MCP read bridge from this Runtime only",
            statement=agentcore.PolicyStatement.from_cedar(
                f'permit(principal == {runtime_principal}, action == AgentCore::Action::"awstoolkit___aws_mcp_read", '
                f'resource == AgentCore::Gateway::"{gw_arn}");'),
            validation_mode=agentcore.PolicyValidationMode.FAIL_ON_ANY_FINDINGS)
        for p in (p_read, p_prop, p_forbid, p_docs, p_toolkit):
            p.node.add_dependency(t_read, t_prop, t_decoy, t_docs, t_toolkit)

        # Gateway observability: application logs -> CloudWatch Logs, traces (incl. policy spans) -> X-Ray/aws/spans
        gw_logs = logs.LogGroup(self, "GatewayVendedLogs", log_group_name="/aws/vendedlogs/bedrock-agentcore/gateway/aiops-ops-tools",
                                retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY)
        src_logs = logs.CfnDeliverySource(self, "GwLogsSrc", name="aiops-gw-logs", log_type="APPLICATION_LOGS",
                                          resource_arn=gw_arn)
        src_traces = logs.CfnDeliverySource(self, "GwTracesSrc", name="aiops-gw-traces", log_type="TRACES",
                                            resource_arn=gw_arn)
        dst_logs = logs.CfnDeliveryDestination(self, "GwLogsDst", name="aiops-gw-logs-cwl",
                                               destination_resource_arn=gw_logs.log_group_arn)
        dst_traces = logs.CfnDeliveryDestination(self, "GwTracesDst", name="aiops-gw-traces-xray",
                                                 delivery_destination_type="XRAY")
        d1 = logs.CfnDelivery(self, "GwLogsDelivery", delivery_source_name=src_logs.name,
                              delivery_destination_arn=dst_logs.attr_arn)
        d1.add_dependency(src_logs)
        d2 = logs.CfnDelivery(self, "GwTracesDelivery", delivery_source_name=src_traces.name,
                              delivery_destination_arn=dst_traces.attr_arn)
        d2.add_dependency(src_traces)
        d2.add_dependency(d1)

        # ------------------------------------------------------------------ approval flow (Step Functions)
        flow_fn = fn("ApprovalFlow", "flow_step", timeout=660)
        ddb_keys(flow_fn.role, ["GetItem", "PutItem", "UpdateItem", "DeleteItem"], ["APR#", "LOCK#", "MOCKSTATE#"])
        flow_fn.role.add_to_policy(iam.PolicyStatement(actions=["ssm:GetAutomationExecution"],
                                                       resources=[f"arn:aws:ssm:{region}:{acct}:automation-execution/*"]))
        flow_fn.role.add_to_policy(read_only_aws)
        memory_write(flow_fn.role)
        deny_mutations(flow_fn.role, ["secretsmanager:*"])

        exec_role = iam.Role(self, "ExecutorRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                             description="Runbook executor: start the allowlisted SSM Automation document only")
        executor_fn = fn("Executor", "executor", timeout=60, role=exec_role, env={
            "AIOPS_AUTOMATION_ASSUME_ROLE_ARN": automation_role.role_arn,
            "AIOPS_EXECUTOR_ALLOWED_INSTANCES": demo.instance_id, "AIOPS_TARGET_REGION": region,
            "AIOPS_EXECUTOR_PRINCIPAL": "role/aiops-executor (Lambda)"})
        exec_role.add_to_policy(iam.PolicyStatement(
            sid="StartOnlyTheAllowlistedDocument", actions=["ssm:StartAutomationExecution"],
            resources=[f"arn:aws:ssm:{region}::document/{ALLOWED_DOCUMENT}",
                       f"arn:aws:ssm:{region}::automation-definition/{ALLOWED_DOCUMENT}:*",
                       f"arn:aws:ssm:{region}:{acct}:automation-definition/{ALLOWED_DOCUMENT}:*",
                       f"arn:aws:ssm:{region}:{acct}:automation-execution/*"]))
        exec_role.add_to_policy(iam.PolicyStatement(
            sid="TagExecutionsWithApprovalId", actions=["ssm:AddTagsToResource"],
            resources=[f"arn:aws:ssm:{region}:{acct}:automation-execution/*"]))
        exec_role.add_to_policy(iam.PolicyStatement(
            sid="PassOnlyTheAutomationRole", actions=["iam:PassRole"], resources=[automation_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "ssm.amazonaws.com"}}))
        ddb_keys(exec_role, ["GetItem", "UpdateItem", "DeleteItem"], ["APR#", "LOCK#", "MOCKSTATE#"])

        notifier_fn = fn("Notifier", "notifier", timeout=30, memory_mb=256,
                         env={"AIOPS_SLACK_SECRET_ARN": slack_secret.secret_arn})
        slack_secret.grant_read(notifier_fn.role)
        ddb_keys(notifier_fn.role, ["GetItem"], ["APR#"])

        def lam(name, f, payload, result_path):
            return tasks.LambdaInvoke(self, name, lambda_function=f, payload=sfn.TaskInput.from_object(payload),
                                      payload_response_only=True, result_path=result_path)

        apr = sfn.JsonPath.string_at("$.approval_id")
        wait_decision = tasks.LambdaInvoke(
            self, "WaitForHumanDecision", lambda_function=flow_fn,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object({"step": "register", "approval_id": apr, "token": sfn.JsonPath.task_token}),
            heartbeat_timeout=sfn.Timeout.duration(Duration.minutes(30)),
            task_timeout=sfn.Timeout.duration(Duration.minutes(31)), result_path="$.decision")
        expire = lam("MarkExpired", flow_fn, {"step": "expire", "approval_id": apr,
                                              "thread_id": sfn.JsonPath.string_at("$.thread_id")}, "$.expire")
        notify_expired = lam("NotifyExpired", notifier_fn, {"approval_id": apr, "kind": "expired"}, sfn.JsonPath.DISCARD)
        wait_decision.add_catch(expire.next(notify_expired).next(sfn.Succeed(self, "Expired")),
                                errors=["States.Timeout", "States.HeartbeatTimeout", "NotPending"], result_path="$.error")
        notify_rejected = lam("NotifyRejected", notifier_fn, {"approval_id": apr, "kind": "rejected"}, sfn.JsonPath.DISCARD)
        execute = lam("StartAutomation", executor_fn, {"approval_id": apr}, "$.exec")
        notify_exec_error = lam("NotifyExecError", notifier_fn, {
            "approval_id": apr, "kind": "exec_error", "error": sfn.JsonPath.string_at("$.exec.error"),
            "message": sfn.JsonPath.string_at("$.exec.message")}, sfn.JsonPath.DISCARD)
        notify_crash = lam("NotifyStepError", notifier_fn, {"approval_id": apr, "kind": "result"}, sfn.JsonPath.DISCARD)
        wait_poll = sfn.Wait(self, "WaitForAutomation", time=sfn.WaitTime.duration(Duration.seconds(15)))
        status = lam("GetAutomationStatus", flow_fn, {"step": "status", "approval_id": apr}, "$.status")
        verify = lam("VerifyCompletion", flow_fn, {"step": "verify", "approval_id": apr,
                                                   "started_at": sfn.JsonPath.string_at("$.exec.started_at"),
                                                   "status": sfn.JsonPath.object_at("$.status")}, "$.verify")
        notify_result = lam("NotifyResult", notifier_fn, {"approval_id": apr, "kind": "result"}, sfn.JsonPath.DISCARD)
        crash_end = notify_crash.next(sfn.Fail(self, "StepFailed", error="StepFailed"))
        execute.add_catch(crash_end, result_path="$.error")
        verify.add_catch(crash_end, result_path="$.error")
        wait_poll.next(status).next(sfn.Choice(self, "AutomationFinished?")
                                    .when(sfn.Condition.boolean_equals("$.status.terminal", True),
                                          verify.next(notify_result).next(sfn.Succeed(self, "Done")))
                                    .otherwise(wait_poll))
        definition = wait_decision.next(
            sfn.Choice(self, "Approved?")
            .when(sfn.Condition.string_equals("$.decision.decision", "approve"),
                  execute.next(sfn.Choice(self, "Started?")
                               .when(sfn.Condition.is_present("$.exec.error"),
                                     notify_exec_error.next(sfn.Succeed(self, "NotExecuted")))
                               .otherwise(wait_poll)))
            .otherwise(notify_rejected.next(sfn.Succeed(self, "Rejected"))))
        sfn_logs = logs.LogGroup(self, "ApprovalFlowSfnLogs", log_group_name="/aws/vendedlogs/states/aiops-approval-flow",
                                 retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY)
        state_machine = sfn.StateMachine(
            self, "ApprovalFlow-SM", state_machine_name="aiops-approval-flow",
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(definition), timeout=Duration.hours(2),
            tracing_enabled=True, logs=sfn.LogOptions(destination=sfn_logs, level=sfn.LogLevel.ALL,
                                                      include_execution_data=False))
        # by name (not the token) to avoid a cycle: flow role -> state machine -> flow Lambda -> flow role
        sm_arn_by_name = f"arn:aws:states:{region}:{acct}:stateMachine:aiops-approval-flow"
        flow_fn.role.add_to_policy(iam.PolicyStatement(actions=["states:SendTaskSuccess", "states:SendTaskFailure",
                                                                "states:SendTaskHeartbeat"], resources=[sm_arn_by_name]))
        gw_propose.add_environment("AIOPS_STATE_MACHINE_ARN", state_machine.state_machine_arn)
        gw_propose.add_environment("AIOPS_MEMORY_ID", memory.memory_id)
        flow_fn.add_environment("AIOPS_MEMORY_ID", memory.memory_id)
        state_machine.grant_start_execution(gw_propose.role)

        # ------------------------------------------------------------------ Slack ingress, worker, interactivity
        slack_env = {"AIOPS_SLACK_SECRET_ARN": slack_secret.secret_arn}
        # Slack-facing functions: 1024 MB (more CPU for the cold start) + init-phase warm-up (AIOPS_PREWARM)
        ingress = fn("SlackIngress", "slack_ingress", timeout=10, memory_mb=1024,
                     env={**slack_env, "AIOPS_QUEUE_URL": queue.queue_url, "AIOPS_PREWARM": "slack-ingress"})
        slack_secret.grant_read(ingress.role)
        queue.grant_send_messages(ingress.role)
        ddb_keys(ingress.role, ["PutItem", "GetItem"], ["EVT#"])

        worker = fn("SlackWorker", "slack_worker", timeout=900, memory_mb=256,
                    env={**slack_env, "AIOPS_RUNTIME_ARN": runtime.agent_runtime_arn})
        slack_secret.grant_read(worker.role)
        ddb_keys(worker.role, ["GetItem", "UpdateItem"], ["APR#"])
        worker.role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[runtime.agent_runtime_arn, f"{runtime.agent_runtime_arn}/runtime-endpoint/*"]))
        worker.add_event_source(les.SqsEventSource(queue, batch_size=1, report_batch_item_failures=True))

        interactivity = fn("SlackInteractivity", "slack_interactivity", timeout=10, memory_mb=1024, env={
            **slack_env, "AIOPS_APPROVERS_PARAM": approvers_param.parameter_name, "AIOPS_MEMORY_ID": memory.memory_id,
            "AIOPS_PREWARM": "slack-interactivity"})
        slack_secret.grant_read(interactivity.role)
        approvers_param.grant_read(interactivity.role)
        ddb_keys(interactivity.role, ["GetItem", "UpdateItem", "DeleteItem"], ["APR#", "LOCK#"])
        ddb_keys(interactivity.role, ["GetItem"], ["EVT#prewarm"])
        state_machine.grant_task_response(interactivity.role)
        memory_write(interactivity.role)
        deny_mutations(interactivity.role)

        api = apigw.HttpApi(self, "SlackApi", api_name="aiops-slack", description="Slack Events + Interactivity")
        api.add_routes(path="/slack/events", methods=[apigw.HttpMethod.POST],
                       integration=apigw_int.HttpLambdaIntegration("EventsInt", ingress))
        api.add_routes(path="/slack/interactivity", methods=[apigw.HttpMethod.POST],
                       integration=apigw_int.HttpLambdaIntegration("InteractivityInt", interactivity))
        api_logs = logs.LogGroup(self, "ApiAccessLogs", log_group_name="/aws/apigateway/aiops-slack",
                                 retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY)
        stage = api.default_stage.node.default_child
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(throttling_burst_limit=20,
                                                                             throttling_rate_limit=10)
        stage.access_log_settings = apigw.CfnStage.AccessLogSettingsProperty(
            destination_arn=api_logs.log_group_arn,
            format=json.dumps({"requestId": "$context.requestId", "routeKey": "$context.routeKey",
                               "status": "$context.status", "latency": "$context.responseLatency",
                               "integrationLatency": "$context.integrationLatency", "ip": "$context.identity.sourceIp"}))

        # ------------------------------------------------------------------ outputs
        for k, v in {"SlackEventsUrl": f"{api.api_endpoint}/slack/events",
                     "SlackInteractivityUrl": f"{api.api_endpoint}/slack/interactivity",
                     "RuntimeArn": runtime.agent_runtime_arn, "RuntimeId": runtime.agent_runtime_id,
                     "RuntimeRoleName": runtime_role.role_name,
                     "GatewayUrl": gateway.gateway_url, "GatewayArn": gw_arn, "GatewayId": gateway.gateway_id,
                     "PolicyEngineId": policy_engine.policy_engine_id, "MemoryId": memory.memory_id,
                     "TableName": table.table_name, "QueueUrl": queue.queue_url, "DlqUrl": dlq.queue_url,
                     "StateMachineArn": state_machine.state_machine_arn, "SlackSecretArn": slack_secret.secret_arn,
                     "ApproversParam": approvers_param.parameter_name, "DemoInstanceId": demo.instance_id,
                     "AutomationRoleArn": automation_role.role_arn,
                     "GatewayTargets": gateway_targets}.items():
            CfnOutput(self, k, value=v)
