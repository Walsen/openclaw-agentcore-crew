"""Token Monitoring Stack — usage tracking and budget enforcement.

Creates:
  - DynamoDB table for token usage records
  - Lambda processor that aggregates usage and enforces daily budgets
  - CloudWatch dashboard widget for token analytics
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct

from stacks.router_stack import RouterStack


class TokenMonitoringStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        router_stack: RouterStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("stack_prefix") or "OpenClaw"
        token_ttl = self.node.try_get_context("token_ttl_days") or 90
        daily_budget = self.node.try_get_context("daily_token_budget") or 1000000
        cost_budget = self.node.try_get_context("daily_cost_budget_usd") or 10
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30

        # --- Token usage table --------------------------------------------
        self.usage_table = dynamodb.Table(
            self,
            "UsageTable",
            table_name=f"{prefix.lower()}-token-usage",
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            time_to_live_attribute="ttl",
        )

        # --- Token processor Lambda ---------------------------------------
        self.processor_function = lambda_.Function(
            self,
            "ProcessorFunction",
            function_name=f"{prefix.lower()}-token-processor",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambda/token_processor"),
            timeout=cdk.Duration.seconds(60),
            memory_size=128,
            architecture=lambda_.Architecture.ARM_64,
            environment={
                "USAGE_TABLE": self.usage_table.table_name,
                "IDENTITY_TABLE": router_stack.identity_table.table_name,
                "DAILY_TOKEN_BUDGET": str(daily_budget),
                "DAILY_COST_BUDGET_USD": str(cost_budget),
                "TOKEN_TTL_DAYS": str(token_ttl),
            },
            log_retention=logs.RetentionDays(log_retention),
        )

        self.usage_table.grant_read_write_data(self.processor_function)
        router_stack.identity_table.grant_read_data(self.processor_function)

        # --- Outputs ------------------------------------------------------
        cdk.CfnOutput(self, "UsageTableName", value=self.usage_table.table_name)
        cdk.CfnOutput(
            self, "ProcessorFunctionArn", value=self.processor_function.function_arn
        )
