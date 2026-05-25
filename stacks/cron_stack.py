"""Cron Stack — EventBridge Scheduler and Cron executor Lambda.

Supports OpenClaw's scheduled tasks (reminders, recurring jobs).
EventBridge Scheduler invokes the cron Lambda, which calls AgentCore
to execute the scheduled task in the user's session.
"""

import aws_cdk as cdk
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
    aws_scheduler as scheduler,
)
from constructs import Construct

from stacks.agentcore_stack import AgentCoreStack
from stacks.router_stack import RouterStack


class CronStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        agentcore_stack: AgentCoreStack,
        router_stack: RouterStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("stack_prefix") or "OpenClaw"
        timeout_s = self.node.try_get_context("cron_lambda_timeout_seconds") or 900
        memory_mb = self.node.try_get_context("cron_lambda_memory_mb") or 256

        # --- Cron executor Lambda log group -------------------------------
        cron_log_group = logs.LogGroup(
            self,
            "CronLogGroup",
            log_group_name=f"/aws/lambda/{prefix.lower()}-cron",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # --- Cron executor Lambda -----------------------------------------
        self.cron_function = lambda_.Function(
            self,
            "CronFunction",
            function_name=f"{prefix.lower()}-cron",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambda/cron"),
            timeout=cdk.Duration.seconds(timeout_s),
            memory_size=memory_mb,
            architecture=lambda_.Architecture.ARM_64,
            environment={
                "IDENTITY_TABLE": router_stack.identity_table.table_name,
                "STACK_NAME": prefix,
                "RUNTIME_ID": self.node.try_get_context("runtime_id") or "",
            },
            log_group=cron_log_group,
        )

        # Grant permissions
        router_stack.identity_table.grant_read_data(self.cron_function)

        self.cron_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AgentCoreInvoke",
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeStreaming",
                ],
                resources=["*"],
            )
        )

        # --- EventBridge Scheduler group ----------------------------------
        self.scheduler_group = scheduler.CfnScheduleGroup(
            self,
            "SchedulerGroup",
            name=f"{prefix.lower()}-cron",
        )

        # Scheduler execution role
        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        self.cron_function.grant_invoke(scheduler_role)

        # --- Outputs ------------------------------------------------------
        cdk.CfnOutput(self, "CronFunctionArn", value=self.cron_function.function_arn)
        cdk.CfnOutput(self, "SchedulerGroupName", value=self.scheduler_group.name)
