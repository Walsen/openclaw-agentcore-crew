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
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct

from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack


class RouterStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc_stack: VpcStack,
        security_stack: SecurityStack,
        agentcore_stack: AgentCoreStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("stack_prefix") or "OpenClaw"
        channels = self.node.try_get_context("channels") or ["telegram"]
        timeout_s = self.node.try_get_context("router_lambda_timeout_seconds") or 600
        memory_mb = self.node.try_get_context("router_lambda_memory_mb") or 256
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        max_users = self.node.try_get_context("max_users") or 10
        registration_open = self.node.try_get_context("registration_open")

        # --- DynamoDB identity table --------------------------------------
        self.identity_table = dynamodb.Table(
            self,
            "IdentityTable",
            table_name=f"{prefix.lower()}-identity",
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=security_stack.kms_key,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
        )

        # GSI for looking up users by channel ID
        self.identity_table.add_global_secondary_index(
            index_name="channel-lookup",
            partition_key=dynamodb.Attribute(
                name="channel_id", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # runtime_id is populated in cdk.json after Phase 2 completes
        runtime_id = self.node.try_get_context("runtime_id") or ""

        # --- Router Lambda ------------------------------------------------
        self.router_function = lambda_.Function(
            self,
            "RouterFunction",
            function_name=f"{prefix.lower()}-router",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_asset(
                "lambda/router",
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
                    ],
                ),
            ),
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
                # Per-channel secret ARNs — router uses these to verify
                # signatures and to send replies back to each platform
                **{
                    f"{ch.upper()}_SECRET_ARN": secret.secret_arn
                    for ch, secret in security_stack.channel_secrets.items()
                },
                "WEBHOOK_SECRET_ARN": security_stack.webhook_secret.secret_arn,
            },
            log_retention=logs.RetentionDays(log_retention),
        )

        # Grant Lambda permissions
        self.identity_table.grant_read_write_data(self.router_function)
        security_stack.kms_key.grant_decrypt(self.router_function)
        for secret in security_stack.channel_secrets.values():
            secret.grant_read(self.router_function)
        security_stack.webhook_secret.grant_read(self.router_function)

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
        stage = apigwv2.CfnStage(
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
        cdk.CfnOutput(
            self, "RouterFunctionArn", value=self.router_function.function_arn
        )
