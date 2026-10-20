#!/usr/bin/env python3
"""CDK app: module 3 AI-Ops agent in the production architecture (us-east-1).

  cd demo/ai-ops-slack-agent/infra
  ../.venv-infra/bin/cdk ... or:  cdk deploy -c aiops:approvers=U0XXXX,U0YYYY
Account and region come from the default AWS credentials (CDK_DEFAULT_ACCOUNT / aiops:region context).
No account id or secret is written in this code.
"""

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from aiops_infra.stack import AiopsAgentStack
from aiops_infra.web_stack import AiopsWebStack

IAM5_FINDINGS = [
    "AwsSolutions-IAM5[Resource::*]",
    "AwsSolutions-IAM5[Resource::<AgentRuntime917DDB48.AgentRuntimeArn>/runtime-endpoint/*]",
    "AwsSolutions-IAM5[Resource::<ApprovalFlow7DA6AA72.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<ApprovalFlowLogs463A9EE2.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<ExecutorB07C39A8.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<ExecutorLogs2D9990C3.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<GatewayDecoy6FBA39C8.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<GatewayDecoyLogs937E09E0.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<GatewayProposeEE52B11F.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<GatewayProposeLogs419FC9F6.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<GatewayReadBCA05307.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<GatewayReadLogsFD0FE1B4.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<GatewayToolkit1B7DFC38.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<GatewayToolkitLogsD723672B.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<NotifierDFD80165.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<NotifierLogs49177D36.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<SlackIngressLogs5D31BE54.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<SlackInteractivityLogsFC662B0A.Arn>:*]",
    "AwsSolutions-IAM5[Resource::<SlackWorkerLogs23D2AF37.Arn>:*]",
    "AwsSolutions-IAM5[Resource::arn:<AWS::Partition>:bedrock-agentcore:us-east-1:{account}:workload-identity-directory/default/workload-identity/*]",
    "AwsSolutions-IAM5[Resource::arn:<AWS::Partition>:logs:us-east-1:{account}:log-group:*]",
    "AwsSolutions-IAM5[Resource::arn:<AWS::Partition>:logs:us-east-1:{account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*]",
    "AwsSolutions-IAM5[Resource::arn:<AWS::Partition>:logs:us-east-1:{account}:log-group:/aws/bedrock-agentcore/runtimes/*]",
    "AwsSolutions-IAM5[Resource::arn:aws:ec2:us-east-1:{account}:instance/*]",
    "AwsSolutions-IAM5[Resource::arn:aws:ssm:us-east-1:{account}:automation-definition/AWS-RestartEC2Instance:*]",
    "AwsSolutions-IAM5[Resource::arn:aws:ssm:us-east-1:{account}:automation-execution/*]",
    "AwsSolutions-IAM5[Resource::arn:aws:ssm:us-east-1::automation-definition/AWS-RestartEC2Instance:*]",
]

# Findings from the first cdk-nag (AwsSolutions) run that are accepted, each with the reason (README §9).
ACKS: dict[str, str] = {
    "AwsSolutions::AwsSolutions-IAM5": (
        "Reviewed one by one: wildcards are limited to log streams of each function's own log group, "
        "automation-execution/* (ids are generated per run), Lambda version suffix (:*) for Step Functions, the "
        "AgentCore runtime log groups / workload identity created by the Runtime construct, the demo instance ARN "
        "pattern guarded by tag conditions (Service + aiops:demo-target), read-only Describe/Get/Lookup APIs without "
        "resource-level support, X-Ray, and explicit Deny statements."),
    "AwsSolutions::AwsSolutions-APIG4": (
        "Slack cannot sign requests with IAM/JWT; every request is authenticated in the Lambda by the Slack "
        "signing-secret HMAC with a 5-minute replay window, and the stage is throttled."),
    "AwsSolutions::AwsSolutions-SMG4": "Slack bot token / signing secret are rotated in Slack, not by Secrets Manager.",
    "AwsSolutions::AwsSolutions-EC28": "Single demo target for the SSM path; detailed monitoring adds cost only.",
    "AwsSolutions::AwsSolutions-EC29": "Demo target must be destroyable with cdk destroy; termination protection off.",
    "AwsSolutions::AwsSolutions-L1": "Python 3.12 pinned to the version the unit tests run on.",
}

# AiopsWebStack (Streamlit console: CloudFront VPC origin -> internal ALB -> EC2, no login). Accepted findings.
WEB_ACKS: dict[str, str] = {
    "AwsSolutions::AwsSolutions-IAM4": "AmazonSSMManagedInstanceCore: Session Manager access instead of SSH.",
    "AwsSolutions::AwsSolutions-IAM5": (
        "Wildcards are limited to the stack id suffix of AiopsAgentStack (DescribeStacks), endpoints of this one "
        "Runtime, and executions of this one state machine (ids per run). No Resource '*' permission."),
    "AwsSolutions::AwsSolutions-ELB2": "Workshop demo: ALB access logs need an extra bucket; CloudFront is the only entry.",
    "AwsSolutions::AwsSolutions-EC28": "Single demo instance; detailed monitoring adds cost only.",
    "AwsSolutions::AwsSolutions-EC29": "Demo console must be destroyable with cdk destroy; termination protection off.",
    "AwsSolutions::AwsSolutions-CFR1": "Workshop participants may connect from any country.",
    "AwsSolutions::AwsSolutions-CFR2": "WAF adds fixed monthly cost for a short-lived workshop demo; the ALB is internal (VPC origin).",
    "AwsSolutions::AwsSolutions-CFR3": "Workshop demo: CloudFront access logs need an extra bucket.",
    "AwsSolutions::AwsSolutions-CFR4": "Default *.cloudfront.net certificate (no custom domain) does not allow a TLS policy choice.",
    "AwsSolutions::AwsSolutions-CFR5": "Origin is an internal ALB reached through the CloudFront VPC origin (private), HTTP only.",
}
WEB_GRANULAR = [
    "AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonSSMManagedInstanceCore]",
    "AwsSolutions-IAM5[Resource::arn:aws:cloudformation:{region}:{account}:stack/AiopsAgentStack/*]",
    "AwsSolutions-IAM5[Resource::{RuntimeArn}/runtime-endpoint/*]",
    "AwsSolutions-IAM5[Resource::arn:aws:states:{region}:{account}:execution:{sm_name}:*]",
]


def agent_stack_outputs(name: str, region: str) -> dict[str, str] | None:
    """Read AiopsAgentStack outputs at synth time; the web stack only needs its ARNs (no cross-stack exports)."""
    try:
        import boto3
        stack = boto3.client("cloudformation", region_name=region).describe_stacks(StackName=name)["Stacks"][0]
    except Exception as exc:  # noqa: BLE001 - not deployed yet, no credentials, or boto3 missing
        print(f"[aiops] {name} outputs unavailable ({type(exc).__name__}); AiopsWebStack is skipped.")
        return None
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


app = cdk.App()
region = app.node.try_get_context("aiops:region") or "us-east-1"
account = os.environ.get("CDK_DEFAULT_ACCOUNT")
if not account:
    raise SystemExit("CDK_DEFAULT_ACCOUNT is not set: run through the cdk CLI with valid AWS credentials")
stack = AiopsAgentStack(app, "AiopsAgentStack", env=cdk.Environment(account=account, region=region),
                        description="Workshop module 3: AI-Ops Slack agent on AgentCore (Runtime, Gateway+Policy, Memory) "
                                    "+ DynamoDB/Step Functions approvals")
if app.node.try_get_context("aiops:nag") != "false":
    # cdk-nag v3: rules run as a CDK validation plugin; findings we accept are acknowledged with a reason.
    cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app, verbose=True))
    for rule, reason in ACKS.items():
        cdk.Validations.of(stack).acknowledge(cdk.Acknowledgment(id=rule, reason=reason))
    for finding in IAM5_FINDINGS:  # granular IAM5 findings must be acknowledged one by one
        cdk.Validations.of(stack).acknowledge(cdk.Acknowledgment(
            id="AwsSolutions::" + finding.replace("{account}", account), reason=ACKS["AwsSolutions::AwsSolutions-IAM5"]))

if app.node.try_get_context("aiops:web") != "false":
    outputs = agent_stack_outputs("AiopsAgentStack", region)
    if outputs:
        web = AiopsWebStack(app, "AiopsWebStack", agent=outputs, env=cdk.Environment(account=account, region=region),
                            description="Workshop module 3: Streamlit operator console (CloudFront VPC origin -> "
                                        "internal ALB -> EC2), no login")
        if app.node.try_get_context("aiops:nag") != "false":
            for rule, reason in WEB_ACKS.items():
                cdk.Validations.of(web).acknowledge(cdk.Acknowledgment(id=rule, reason=reason))
            values = dict(outputs, account=account, region=region, sm_name=outputs["StateMachineArn"].rsplit(":", 1)[1])
            for finding in WEB_GRANULAR:  # granular findings are acknowledged one by one
                rule = finding.split("[", 1)[0]
                cdk.Validations.of(web).acknowledge(cdk.Acknowledgment(
                    id="AwsSolutions::" + finding.format(**values), reason=WEB_ACKS["AwsSolutions::" + rule]))
app.synth()
