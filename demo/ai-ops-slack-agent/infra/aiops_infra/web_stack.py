"""AiopsWebStack: the Streamlit operator console behind CloudFront -> internal ALB (VPC origin) -> EC2.

  browser ──HTTPS──▶ CloudFront (CachingDisabled, AllViewer, ALL methods, WebSocket)
                        │ VPC origin (private ENI, no internet-facing ALB)
                        ▼
                     internal ALB :80 (SG: CloudFront origin-facing prefix list only, idle 15 min)
                        ▼
                     EC2 t4g.small :8501 (SG: ALB only, no SSH; SSM Session Manager; IMDSv2; encrypted EBS)
                        └─ systemd: streamlit run streamlit_app.py

No login: anyone with the URL can run investigations (Bedrock cost) and press approve, which restarts the demo
EC2. The approver recorded is the STS caller, i.e. this instance role. Destroy the stack when not in use.

The AiopsAgentStack is not modified: its outputs are read at synth time (app.py) and only used as ARNs here.
"""

from __future__ import annotations

import hashlib
import textwrap

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    Tags,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_targets as targets,
    aws_iam as iam,
    aws_s3_assets as s3_assets,
)
from constructs import Construct

from .bundle import DEMO_DIR

PORT = 8501
# Not shipped to the instance: local state, virtualenvs, build output, secrets and the CDK app itself.
ASSET_EXCLUDE = [".venv", ".venv-infra", "var", "cdk.out", "build", ".env", ".env.*", "cdk.context.json",
                 "__pycache__", "*.pyc", ".pytest_cache", ".git", "infra", "evals/results",
                 ".streamlit/secrets.toml", "config/services.toml"]
# VPC origins are not supported in use1-az3; the ALB needs two AZs.
ALB_AZS = ["us-east-1a", "us-east-1b"]
CLOUDFRONT_PREFIX_LIST = "com.amazonaws.global.cloudfront.origin-facing"


class AiopsWebStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, agent: dict[str, str], agent_stack: str = "AiopsAgentStack",
                 **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)
        acct, region = self.account, self.region
        Tags.of(self).add("project", "aiops-workshop-module3")
        suffix = hashlib.sha256(f"{acct}/{region}/{cid}".encode()).hexdigest()[:8]

        # ------------------------------------------------------------------ network
        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)
        cf_prefix_list = ec2.PrefixList.from_lookup(self, "CloudFrontOriginFacing",
                                                    prefix_list_name=CLOUDFRONT_PREFIX_LIST)
        alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=vpc, allow_all_outbound=False,
                                   description="aiops web ALB: HTTP from CloudFront origin-facing prefix list only")
        alb_sg.add_ingress_rule(ec2.Peer.prefix_list(cf_prefix_list.prefix_list_id), ec2.Port.tcp(80),
                                "CloudFront VPC origin")
        app_sg = ec2.SecurityGroup(self, "AppSg", vpc=vpc, allow_all_outbound=False,
                                   description="aiops web console: 8501 from the ALB only, no SSH")
        app_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(PORT), "Streamlit from the ALB")
        alb_sg.add_egress_rule(app_sg, ec2.Port.tcp(PORT), "to Streamlit")
        app_sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443),
                               "HTTPS to AWS APIs and package repositories")

        # ------------------------------------------------------------------ app code + instance role
        code = s3_assets.Asset(self, "AppCode", path=str(DEMO_DIR), exclude=ASSET_EXCLUDE)
        role = iam.Role(self, "AppRole", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
                        description="aiops web console: the GUI operator permissions of 40-04 only")
        role.add_to_policy(iam.PolicyStatement(sid="ReadThisCodeBundleOnly", actions=["s3:GetObject"],
                                               resources=[code.bucket.arn_for_objects(code.s3_object_key)]))
        self._operator_permissions(role, agent, agent_stack)

        # ------------------------------------------------------------------ ALB (internal)
        alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", vpc=vpc, internet_facing=False, security_group=alb_sg,
            vpc_subnets=ec2.SubnetSelection(availability_zones=ALB_AZS, subnet_type=ec2.SubnetType.PUBLIC),
            idle_timeout=Duration.minutes(15), drop_invalid_header_fields=True)

        # ------------------------------------------------------------------ CloudFront (VPC origin)
        distribution = cloudfront.Distribution(
            self, "Cdn", comment="aiops Streamlit operator console",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100, http_version=cloudfront.HttpVersion.HTTP2,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.VpcOrigin.with_application_load_balancer(
                    alb, http_port=80, protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                    read_timeout=Duration.seconds(60), keepalive_timeout=Duration.seconds(60),
                    vpc_origin_name=f"aiops-web-{suffix}"),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL, compress=False))
        base_url = f"https://{distribution.distribution_domain_name}"

        # ------------------------------------------------------------------ EC2
        user_data = ec2.UserData.for_linux()
        bundle = user_data.add_s3_download_command(bucket=code.bucket, bucket_key=code.s3_object_key,
                                                   local_file="/tmp/aiops-app.zip")
        user_data.add_commands(*self._bootstrap(bundle, region))
        instance = ec2.Instance(
            self, "App", vpc=vpc, instance_type=ec2.InstanceType("t4g.small"),
            vpc_subnets=ec2.SubnetSelection(availability_zones=[ALB_AZS[0]], subnet_type=ec2.SubnetType.PUBLIC),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(cpu_type=ec2.AmazonLinuxCpuType.ARM_64),
            security_group=app_sg, role=role, require_imdsv2=True, associate_public_ip_address=True,
            ssm_session_permissions=True, user_data=user_data, user_data_causes_replacement=True,
            block_devices=[ec2.BlockDevice(device_name="/dev/xvda", volume=ec2.BlockDeviceVolume.ebs(
                16, encrypted=True, volume_type=ec2.EbsDeviceVolumeType.GP3, delete_on_termination=True))])
        Tags.of(instance).add("Name", "aiops-web-console")

        tg = elbv2.ApplicationTargetGroup(
            self, "AppTargets", vpc=vpc, port=PORT, protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[targets.InstanceTarget(instance, PORT)], deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(path="/_stcore/health", healthy_http_codes="200",
                                           interval=Duration.seconds(15), healthy_threshold_count=2,
                                           unhealthy_threshold_count=3))
        alb.add_listener("Http", port=80, protocol=elbv2.ApplicationProtocol.HTTP, open=False,
                         default_target_groups=[tg])

        for k, v in {"Url": base_url, "HealthUrl": f"{base_url}/_stcore/health",
                     "DistributionId": distribution.distribution_id, "InstanceId": instance.instance_id,
                     "AlbDnsName": alb.load_balancer_dns_name, "InstanceRoleArn": role.role_arn}.items():
            CfnOutput(self, k, value=v)

    # ---------------------------------------------------------------------------------------------- helpers
    def _operator_permissions(self, role: iam.Role, agent: dict[str, str], agent_stack: str) -> None:
        """40-04 "GUI를 실행하는 운영자": exactly the calls aiops/console.py makes, on this deployment's ARNs."""
        acct, region = self.account, self.region
        runtime, gateway = agent["RuntimeArn"], agent["GatewayArn"]
        memory = f"arn:aws:bedrock-agentcore:{region}:{acct}:memory/{agent['MemoryId']}"
        table = f"arn:aws:dynamodb:{region}:{acct}:table/{agent['TableName']}"
        state_machine = agent["StateMachineArn"]
        sm_name = state_machine.rsplit(":", 1)[1]
        statements = [
            ("ReadAgentStackOutputs", ["cloudformation:DescribeStacks"],
             [f"arn:aws:cloudformation:{region}:{acct}:stack/{agent_stack}/*"], None),
            ("InvokeTheRuntime", ["bedrock-agentcore:InvokeAgentRuntime"],
             [runtime, f"{runtime}/runtime-endpoint/*"], None),
            ("DeploymentStatus", ["bedrock-agentcore:GetAgentRuntime"], [runtime], None),
            ("GatewayStatus", ["bedrock-agentcore:GetGateway", "bedrock-agentcore:ListGatewayTargets"], [gateway], None),
            ("MemoryReadAndActionEvents", ["bedrock-agentcore:GetMemory", "bedrock-agentcore:ListEvents",
                                           "bedrock-agentcore:CreateEvent"], [memory], None),
            ("ApprovalsConditionalUpdateAndLockRelease",
             ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"], [table],
             {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["APR#*", "LOCK#*"]}}),
            ("ApprovalCallback", ["states:SendTaskSuccess"], [state_machine], None),
            ("ExecutionStatus", ["states:DescribeExecution"],
             [f"arn:aws:states:{region}:{acct}:execution:{sm_name}:*"], None),
        ]
        for sid, actions, resources, conditions in statements:
            role.add_to_policy(iam.PolicyStatement(sid=sid, actions=actions, resources=resources,
                                                   conditions=conditions))

    @staticmethod
    def _bootstrap(bundle: str, region: str) -> list[str]:
        unit = textwrap.dedent(f"""\
            [Unit]
            Description=aiops Streamlit operator console
            After=network-online.target
            Wants=network-online.target

            [Service]
            User=aiops
            WorkingDirectory=/opt/aiops/app
            Environment=HOME=/opt/aiops AWS_REGION={region} AWS_DEFAULT_REGION={region}
            ExecStart=/opt/aiops/venv/bin/streamlit run streamlit_app.py --server.address 0.0.0.0 \\
                --server.port {PORT} --server.headless true --server.fileWatcherType none --server.runOnSave false
            Restart=always
            RestartSec=5

            [Install]
            WantedBy=multi-user.target
            """)
        return [
            "set -euo pipefail",
            "dnf install -y python3.12 python3.12-pip unzip",
            "id aiops >/dev/null 2>&1 || useradd --system --home-dir /opt/aiops --shell /sbin/nologin aiops",
            "rm -rf /opt/aiops/app && mkdir -p /opt/aiops/app",
            f"unzip -q {bundle} -d /opt/aiops/app && rm -f {bundle}",
            "python3.12 -m venv /opt/aiops/venv",
            "/opt/aiops/venv/bin/pip install --quiet --no-cache-dir -r /opt/aiops/app/requirements-ui.txt",
            "mkdir -p /opt/aiops/app/var && chown -R aiops:aiops /opt/aiops && chmod 700 /opt/aiops",
            "cat > /etc/systemd/system/aiops-web.service <<'UNITEOF'\n" + unit + "UNITEOF",
            "systemctl daemon-reload && systemctl enable --now aiops-web.service",
        ]
