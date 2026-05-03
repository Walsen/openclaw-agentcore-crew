"""AgentCore Stack — IAM role, S3 workspace bucket, security group.

This stack creates the resources that the AgentCore Runtime needs:
  - IAM execution role with permissions for Bedrock, S3, DynamoDB, Secrets
  - S3 bucket for per-user workspace persistence
  - Security group for the AgentCore microVMs
  - CfnOutput values consumed by the deploy script and Phase 3 stacks

The AgentCore Runtime itself (CfnRuntime + CfnRuntimeEndpoint) is created
by the agentcore CLI in Phase 2, not by this CDK stack.
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_iam as iam,
    aws_s3 as s3,
    aws_ec2 as ec2,
)
from constructs import Construct

from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.guardrails_stack import GuardrailsStack


class AgentCoreStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc_stack: VpcStack,
        security_stack: SecurityStack,
        guardrails_stack: GuardrailsStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("stack_prefix") or "OpenClaw"
        model_id = self.node.try_get_context("default_model_id")
        docker_image = self.node.try_get_context("docker_image")
        user_files_ttl = self.node.try_get_context("user_files_ttl_days") or 365

        # --- S3 workspace bucket ------------------------------------------
        self.workspace_bucket = s3.Bucket(
            self,
            "WorkspaceBucket",
            bucket_name=f"{prefix.lower()}-workspaces-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=security_stack.kms_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireOldVersions",
                    noncurrent_version_expiration=cdk.Duration.days(30),
                ),
                s3.LifecycleRule(
                    id="ExpireUserFiles",
                    expiration=cdk.Duration.days(user_files_ttl),
                    prefix="workspaces/",
                ),
            ],
        )

        # --- Security group for AgentCore microVMs ------------------------
        self.security_group = ec2.SecurityGroup(
            self,
            "AgentCoreSG",
            vpc=vpc_stack.vpc,
            description="OpenClaw AgentCore microVM security group",
            allow_all_outbound=True,
        )

        # --- IAM execution role -------------------------------------------
        self.execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name=f"{prefix}-AgentCoreExecution",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                iam.ServicePrincipal("bedrock.amazonaws.com"),
            ),
            description="Role assumed by AgentCore Runtime to run OpenClaw containers",
        )

        # Bedrock model invocation
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvoke",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                ],
            )
        )

        # Bedrock guardrails
        if guardrails_stack.guardrail_id:
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    sid="BedrockGuardrails",
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[
                        f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/*"
                    ],
                )
            )

        # S3 workspace access
        self.workspace_bucket.grant_read_write(self.execution_role)

        # KMS decrypt for secrets
        security_stack.kms_key.grant_decrypt(self.execution_role)

        # Secrets Manager read
        for secret in security_stack.channel_secrets.values():
            secret.grant_read(self.execution_role)
        security_stack.webhook_secret.grant_read(self.execution_role)

        # DynamoDB (identity table created in router stack — grant later)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="DynamoDBAccess",
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:Query",
                ],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/openclaw-*"
                ],
            )
        )

        # CloudWatch Logs
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/agentcore/*"
                ],
            )
        )

        # --- Environment variables for the container ----------------------
        guardrail_id = guardrails_stack.guardrail_id or ""
        guardrail_version = guardrails_stack.guardrail_version or ""

        self.container_env = {
            "S3_BUCKET": self.workspace_bucket.bucket_name,
            "STACK_NAME": prefix,
            "AWS_REGION": self.region,
            "BEDROCK_MODEL_ID": model_id,
            "GUARDRAIL_ID": guardrail_id,
            "GUARDRAIL_VERSION": guardrail_version,
        }

        # --- Outputs ------------------------------------------------------
        cdk.CfnOutput(self, "ExecutionRoleArn", value=self.execution_role.role_arn)
        cdk.CfnOutput(self, "WorkspaceBucketName", value=self.workspace_bucket.bucket_name)
        cdk.CfnOutput(self, "SecurityGroupId", value=self.security_group.security_group_id)
        cdk.CfnOutput(self, "DockerImage", value=docker_image)
        cdk.CfnOutput(
            self,
            "PrivateSubnetIds",
            value=",".join(
                [s.subnet_id for s in vpc_stack.vpc.private_subnets]
            ),
        )
