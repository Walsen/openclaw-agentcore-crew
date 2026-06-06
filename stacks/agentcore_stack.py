"""AgentCore Stack — IAM role and S3 workspace bucket.

This stack creates the resources that the AgentCore Runtime needs:
  - IAM execution role with permissions for Bedrock, S3, DynamoDB, Secrets
  - S3 bucket for per-user workspace persistence
  - Google OAuth environment variables for Gmail/Calendar/Drive integration

The AgentCore Runtime runs in PUBLIC network mode (fully managed by AWS),
so no VPC, security groups, or subnets are needed here.

The AgentCore Runtime itself is created by the deploy script in Phase 2.
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_s3 as s3,
)
from constructs import Construct

from stacks.guardrails_stack import GuardrailsStack


class AgentCoreStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        guardrails_stack: GuardrailsStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("stack_prefix") or "OpenClaw"
        model_id = self.node.try_get_context("default_model_id") or "us.anthropic.claude-sonnet-4-20250514-v1:0"
        docker_image = self.node.try_get_context("docker_image") or "ffactory/openclaw:latest"
        user_files_ttl = self.node.try_get_context("user_files_ttl_days") or 365

        # --- S3 workspace bucket ------------------------------------------
        # Import KMS key ARN via CloudFormation export to avoid cross-stack
        # cyclic dependency. SecurityStack exports "OpenClaw-KmsKeyArn".
        self.workspace_bucket = s3.Bucket(
            self,
            "WorkspaceBucket",
            bucket_name=f"{prefix.lower()}-workspaces-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.KMS_MANAGED,
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

        # --- DynamoDB application table -----------------------------------
        # The runtime container (server.py, permissions.py, skill_loader.py,
        # workspace_assembler.py) uses a SINGLE-TABLE design keyed by
        # PK (HASH) / SK (RANGE), both String. It reads/writes items such as
        # ORG#acme / CONFIG#*, CONV#*, SESSION#*, AUDIT#*, EMP#*, POS#*,
        # MAPPING#*, KB#*. The table name defaults to STACK_NAME (the prefix)
        # because the runtime resolves DYNAMODB_TABLE -> STACK_NAME.
        #
        # NOTE: this is DISTINCT from the router's `<prefix>-identity` table
        # (lowercase pk/sk, USER#/PROFILE schema). They are different data
        # models and must not be conflated.
        self.app_table = dynamodb.Table(
            self,
            "AppTable",
            table_name=prefix,  # = STACK_NAME the runtime expects (e.g. "OpenClaw")
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            removal_policy=cdk.RemovalPolicy.RETAIN,
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

        # Bedrock model invocation — allow cross-region inference profiles
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvoke",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    # Foundation models in any region (needed for cross-region inference)
                    "arn:aws:bedrock:*::foundation-model/*",
                    # Cross-region inference profiles
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                    # Application inference profiles
                    f"arn:aws:bedrock:{self.region}:{self.account}:application-inference-profile/*",
                ],
            )
        )

        # Bedrock guardrails
        if guardrails_stack.guardrail_id:
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    sid="BedrockGuardrails",
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/*"],
                )
            )

        # S3 workspace access
        self.workspace_bucket.grant_read_write(self.execution_role)

        # KMS — allow decrypt for S3 KMS_MANAGED key and the CMK
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="KmsDecrypt",
                actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                resources=["*"],
                conditions={
                    "StringLike": {
                        "kms:ViaService": [
                            f"s3.{self.region}.amazonaws.com",
                            f"secretsmanager.{self.region}.amazonaws.com",
                        ]
                    }
                },
            )
        )

        # Secrets Manager read — use ARN wildcards to avoid cross-stack refs
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="SecretsRead",
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:openclaw/*"],
            )
        )

        # DynamoDB
        #  - The runtime's application table (self.app_table, named after the
        #    prefix e.g. "OpenClaw"). The `openclaw-*` wildcard below does NOT
        #    match it (case-sensitive + no dash), so grant it explicitly.
        #  - The `openclaw-*` wildcard covers the router's `<prefix>-identity`
        #    table and any future lowercase-dashed tables.
        self.app_table.grant_read_write_data(self.execution_role)
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
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/openclaw-*",
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/openclaw-*/index/*",
                ],
            )
        )

        # ECR — required for AgentCore to pull the container image (PUBLIC mode)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrPull",
                actions=[
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                resources=["*"],
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
                resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/agentcore/*"],
            )
        )

        # --- Environment variables for the container ----------------------
        guardrail_id = guardrails_stack.guardrail_id or ""
        guardrail_version = guardrails_stack.guardrail_version or ""

        # Google OAuth credentials are stored in Secrets Manager and injected
        # at runtime via environment variables read by the gog (gogcli) skill.
        # The secret ARN is resolved at deploy time; the actual credential
        # values are fetched by the container on first use.
        google_secret_arn = f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:openclaw/google-oauth"

        self.container_env = {
            "S3_BUCKET": self.workspace_bucket.bucket_name,
            "STACK_NAME": prefix,
            "AWS_REGION": self.region,
            "BEDROCK_MODEL_ID": model_id,
            "GUARDRAIL_ID": guardrail_id,
            "GUARDRAIL_VERSION": guardrail_version,
            # Google OAuth — values resolved from Secrets Manager at container
            # startup by the entrypoint or gog skill initialisation.
            # GOG_CREDENTIALS_SECRET_ARN tells the skill where to fetch creds.
            "GOG_CREDENTIALS_SECRET_ARN": google_secret_arn,
            # GOG_ACCOUNT is the Google account email; populated by setup-google
            # and stored in the secret JSON under the "account" key.
            # The container entrypoint reads the secret and exports these vars.
            "GOG_ACCOUNT": self.node.try_get_context("google_account") or "",
        }

        # --- Outputs ------------------------------------------------------
        cdk.CfnOutput(self, "ExecutionRoleArn", value=self.execution_role.role_arn)
        cdk.CfnOutput(self, "WorkspaceBucketName", value=self.workspace_bucket.bucket_name)
        cdk.CfnOutput(self, "AppTableName", value=self.app_table.table_name)
        cdk.CfnOutput(self, "DockerImage", value=docker_image)
        cdk.CfnOutput(
            self,
            "GoogleSecretArn",
            value=google_secret_arn,
            description="Google OAuth secret — run `just setup-google` to populate",
        )
