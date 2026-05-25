"""Observability Stack — CloudWatch dashboards, alarms, and SNS topic.

Provides operational visibility into the OpenClaw deployment:
  - SNS topic for alarm notifications
  - CloudWatch alarms for Lambda errors and DynamoDB throttling
  - A basic operational dashboard
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_sns as sns,
)
from constructs import Construct


class ObservabilityStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("stack_prefix") or "OpenClaw"

        # --- SNS topic for alarms -----------------------------------------
        self.alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name=f"{prefix.lower()}-alarms",
            display_name=f"{prefix} Operational Alarms",
        )

        cdk.CfnOutput(
            self,
            "AlarmTopicArn",
            value=self.alarm_topic.topic_arn,
            description="Subscribe your email/Slack to this topic for alerts",
        )

        # --- Dashboard (placeholder — populated after Phase 3) ------------
        self.dashboard = cloudwatch.Dashboard(
            self,
            "Dashboard",
            dashboard_name=f"{prefix}-Operations",
        )
