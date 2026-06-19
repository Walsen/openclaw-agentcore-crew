"""Router Stack — API Gateway, Router Lambda, DynamoDB identity table.

The Router Lambda:
  - Receives webhooks from Telegram, Slack, WhatsApp, Discord
  - Validates webhook signatures
  - Resolves user identity from DynamoDB
  - Calls AgentCore InvokeAgentRuntime to route the message

API Gateway HTTP API exposes the webhook endpoints.
DynamoDB stores the user identity/allowlist table.
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from constructs import Construct

from stacks.agentcore_stack import AgentCoreStack

# SSM parameter that Phase 2 (scripts/cli.py) writes with the AgentCore runtime id,
# so Phase 3 stacks can resolve it at deploy time without a committed cdk.json value.
RUNTIME_ID_PARAM = "/openclaw/runtime-id"


class RouterStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        agentcore_stack: AgentCoreStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("stack_prefix") or "OpenClaw"
        channels = self.node.try_get_context("channels") or ["telegram"]
        timeout_s = self.node.try_get_context("router_lambda_timeout_seconds") or 60
        memory_mb = self.node.try_get_context("router_lambda_memory_mb") or 256
        max_users = self.node.try_get_context("max_users") or 10
        registration_open = self.node.try_get_context("registration_open") or False

        # --- DynamoDB identity table --------------------------------------
        self.identity_table = dynamodb.Table(
            self,
            "IdentityTable",
            table_name=f"{prefix.lower()}-identity",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

        # GSI for looking up users by channel ID
        self.identity_table.add_global_secondary_index(
            index_name="channel-lookup",
            partition_key=dynamodb.Attribute(name="channel_id", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Runtime id: prefer an explicit cdk.json context override; otherwise read
        # the value Phase 2 writes to SSM (/openclaw/runtime-id), resolved at deploy.
        runtime_id = self.node.try_get_context("runtime_id")
        if not runtime_id:
            runtime_id = ssm.StringParameter.value_for_string_parameter(self, RUNTIME_ID_PARAM)
        runtime_arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/{runtime_id}"

        # --- Router Lambda log group --------------------------------------
        router_log_group = logs.LogGroup(
            self,
            "RouterLogGroup",
            log_group_name=f"/aws/lambda/{prefix.lower()}-router",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # --- Router Lambda ------------------------------------------------
        self.router_function = lambda_.Function(
            self,
            "RouterFunction",
            function_name=f"{prefix.lower()}-router",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambda/router"),
            timeout=cdk.Duration.seconds(timeout_s),
            memory_size=memory_mb,
            architecture=lambda_.Architecture.ARM_64,
            environment={
                "IDENTITY_TABLE": self.identity_table.table_name,
                "WORKSPACE_BUCKET": agentcore_stack.workspace_bucket.bucket_name,
                "STACK_NAME": prefix,
                "CHANNELS": ",".join(channels),
                "MAX_USERS": str(max_users),
                "REGISTRATION_OPEN": str(registration_open).lower(),
                "RUNTIME_ID": runtime_id,
                "RUNTIME_ARN": runtime_arn,
                # Secret names — use the name directly, not a wildcard ARN
                **{f"{ch.upper()}_SECRET_ARN": f"openclaw/channels/{ch}" for ch in channels},
                "WEBHOOK_SECRET_ARN": "openclaw/webhook-secret",
            },
            log_group=router_log_group,
        )

        # Grant Lambda permissions — use explicit ARN-based policies to avoid
        # cross-stack dependency cycles with SecurityStack
        self.identity_table.grant_read_write_data(self.router_function)

        # KMS — explicit policy, no cross-stack reference
        self.router_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="KmsDecrypt",
                actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                resources=["*"],
                conditions={"StringLike": {"kms:ViaService": f"secretsmanager.{self.region}.amazonaws.com"}},
            )
        )

        # Secrets Manager — wildcard ARN, no cross-stack object references
        self.router_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="SecretsRead",
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:openclaw/*"],
            )
        )

        # AgentCore invoke permission
        self.router_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AgentCoreInvoke",
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeStreaming",
                ],
                resources=["*"],
            )
        )

        # Runtime-tunable config in SSM (/openclaw/config/*) — read at invocation
        # so operators can change max_users / registration_open without a redeploy.
        self.router_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="SsmConfigRead",
                actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/openclaw/config/*"],
            )
        )

        # --- API Gateway HTTP API -----------------------------------------
        self.http_api = apigwv2.CfnApi(
            self,
            "HttpApi",
            name=f"{prefix}-webhook-api",
            protocol_type="HTTP",
            cors_configuration=apigwv2.CfnApi.CorsProperty(
                allow_methods=["POST", "GET"],
                allow_origins=["*"],
                allow_headers=["Content-Type", "Authorization"],
            ),
        )

        # Lambda integration
        integration = apigwv2.CfnIntegration(
            self,
            "RouterIntegration",
            api_id=self.http_api.ref,
            integration_type="AWS_PROXY",
            integration_uri=self.router_function.function_arn,
            payload_format_version="2.0",
        )

        # Catch-all route
        apigwv2.CfnRoute(
            self,
            "DefaultRoute",
            api_id=self.http_api.ref,
            route_key="$default",
            target=f"integrations/{integration.ref}",
        )

        # Auto-deploy stage
        apigwv2.CfnStage(
            self,
            "DefaultStage",
            api_id=self.http_api.ref,
            stage_name="$default",
            auto_deploy=True,
        )

        # Grant API Gateway permission to invoke the Lambda
        self.router_function.add_permission(
            "ApiGatewayInvoke",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            source_arn=f"arn:aws:execute-api:{self.region}:{self.account}:{self.http_api.ref}/*",
        )

        # --- Outputs ------------------------------------------------------
        self.api_url = f"https://{self.http_api.ref}.execute-api.{self.region}.amazonaws.com"

        cdk.CfnOutput(self, "ApiUrl", value=self.api_url)
        cdk.CfnOutput(self, "IdentityTableName", value=self.identity_table.table_name)
        cdk.CfnOutput(self, "RouterFunctionArn", value=self.router_function.function_arn)
