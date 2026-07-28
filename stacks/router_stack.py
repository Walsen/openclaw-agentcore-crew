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
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_cloudwatch_actions as cw_actions,
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
    aws_lambda_destinations as lambda_destinations,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from constructs import Construct

from stacks.agentcore_stack import AgentCoreStack
from stacks.observability_stack import ObservabilityStack

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
        observability_stack: ObservabilityStack | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("stack_prefix") or "OpenClaw"
        channels = self.node.try_get_context("channels") or ["telegram"]
        # The webhook half of the router answers in ~100ms; this timeout covers the
        # async worker half, which runs a full agent turn (server.py allows 300s).
        timeout_s = self.node.try_get_context("router_lambda_timeout_seconds") or 330
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
            # The router writes one DEDUPE#webhook item per inbound delivery so
            # platform redeliveries can be recognised and dropped. TTL reaps them.
            time_to_live_attribute="expires_at",
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

        # The router hands slow work to an async invocation of itself, so it needs
        # permission to invoke itself. The ARN is built from the known function
        # name rather than function_arn: referencing the construct's own ARN in
        # its own role policy creates a CloudFormation dependency cycle.
        self.router_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="SelfAsyncInvoke",
                actions=["lambda:InvokeFunction"],
                resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:{prefix.lower()}-router"],
            )
        )

        # Dead-letter queue for the async worker. Because the reply is delivered by
        # the worker rather than the webhook response, a worker that dies leaves the
        # user with no answer and nothing else to notice it — the failed job lands
        # here instead of vanishing.
        self.worker_dlq = sqs.Queue(
            self,
            "RouterWorkerDlq",
            queue_name=f"{prefix.lower()}-router-worker-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            retention_period=cdk.Duration.days(14),
        )

        # Async invocations get no automatic retries. Lambda's default of 2 would
        # re-run a non-idempotent agent turn and post duplicate replies — the exact
        # behaviour this change fixes. Failed jobs go to the DLQ instead.
        self.router_function.configure_async_invoke(
            retry_attempts=0,
            on_failure=lambda_destinations.SqsDestination(self.worker_dlq),
        )

        # --- Alarms on silent worker failure ------------------------------
        # "Worker failed" is logged by _handle_worker when an agent turn raises.
        worker_error_metric = router_log_group.add_metric_filter(
            "WorkerFailedFilter",
            filter_pattern=logs.FilterPattern.literal('"Worker failed"'),
            metric_namespace=f"{prefix}/Router",
            metric_name="WorkerFailures",
            metric_value="1",
        ).metric(statistic="Sum", period=cdk.Duration.minutes(5))

        worker_failure_alarm = cloudwatch.Alarm(
            self,
            "WorkerFailureAlarm",
            alarm_name=f"{prefix}-router-worker-failures",
            alarm_description="Router async worker raised — the user received no reply.",
            metric=worker_error_metric,
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        dlq_alarm = cloudwatch.Alarm(
            self,
            "WorkerDlqAlarm",
            alarm_name=f"{prefix}-router-worker-dlq",
            alarm_description="A router worker job was dead-lettered — a message went unanswered.",
            metric=self.worker_dlq.metric_approximate_number_of_messages_visible(
                statistic="Maximum", period=cdk.Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        if observability_stack is not None:
            action = cw_actions.SnsAction(observability_stack.alarm_topic)
            worker_failure_alarm.add_alarm_action(action)
            dlq_alarm.add_alarm_action(action)

        cdk.CfnOutput(self, "WorkerDlqUrl", value=self.worker_dlq.queue_url)

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
