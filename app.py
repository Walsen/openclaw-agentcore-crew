#!/usr/bin/env python3
"""OpenClaw AgentCore Infrastructure — CDK App Entry Point.

Deploys OpenClaw on Amazon Bedrock AgentCore Runtime using a pre-built
Docker image from Docker Hub (ffactory/openclaw:latest).

Deployment phases:
  Phase 1 — Foundation:  VPC, Security, Guardrails, Observability
  Phase 2 — Runtime:     AgentCore Runtime + Endpoint (via agentcore CLI)
  Phase 3 — Application: AgentCore stack, Router, Cron, Token Monitoring
"""

import aws_cdk as cdk

from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.guardrails_stack import GuardrailsStack
from stacks.observability_stack import ObservabilityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.router_stack import RouterStack
from stacks.cron_stack import CronStack
from stacks.token_monitoring_stack import TokenMonitoringStack

app = cdk.App()

account = app.node.try_get_context("account")
region = app.node.try_get_context("region") or "us-east-1"
prefix = app.node.try_get_context("stack_prefix") or "OpenClaw"

env = cdk.Environment(account=account, region=region)

# ---------------------------------------------------------------------------
# Phase 1 — Foundation (no inter-stack dependencies)
# ---------------------------------------------------------------------------

vpc_stack = VpcStack(
    app,
    f"{prefix}Vpc",
    env=env,
    description="OpenClaw — VPC, subnets, NAT, VPC endpoints",
)

security_stack = SecurityStack(
    app,
    f"{prefix}Security",
    env=env,
    description="OpenClaw — KMS, Secrets Manager, Cognito",
)

guardrails_stack = GuardrailsStack(
    app,
    f"{prefix}Guardrails",
    env=env,
    description="OpenClaw — Bedrock Guardrails (content filters, PII, topics)",
)

observability_stack = ObservabilityStack(
    app,
    f"{prefix}Observability",
    env=env,
    description="OpenClaw — CloudWatch dashboards, alarms, SNS",
)

# ---------------------------------------------------------------------------
# Phase 2 — AgentCore Runtime
#
# The runtime itself is deployed via the `agentcore` CLI (not CDK) because
# CfnRuntime requires the container image to already be in ECR.  The deploy
# script handles: pull from Docker Hub → push to ECR → create runtime.
#
# After Phase 2 completes, add runtime_id to cdk.json context, then deploy
# Phase 3.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 3 — Application (depends on Phase 2 runtime_id in cdk.json)
# ---------------------------------------------------------------------------

agentcore_stack = AgentCoreStack(
    app,
    f"{prefix}AgentCore",
    vpc_stack=vpc_stack,
    guardrails_stack=guardrails_stack,
    env=env,
    description="OpenClaw — AgentCore IAM, S3 workspace, Workload Identity",
)

router_stack = RouterStack(
    app,
    f"{prefix}Router",
    agentcore_stack=agentcore_stack,
    env=env,
    description="OpenClaw — Router Lambda, API Gateway, DynamoDB identity",
)

cron_stack = CronStack(
    app,
    f"{prefix}Cron",
    agentcore_stack=agentcore_stack,
    router_stack=router_stack,
    env=env,
    description="OpenClaw — EventBridge Scheduler, Cron executor Lambda",
)

token_monitoring_stack = TokenMonitoringStack(
    app,
    f"{prefix}TokenMonitoring",
    router_stack=router_stack,
    env=env,
    description="OpenClaw — DynamoDB token usage, Lambda processor, dashboard",
)

app.synth()
