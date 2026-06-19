#!/usr/bin/env python3
"""OpenClaw AgentCore Infrastructure — CDK App Entry Point.

Deploys OpenClaw on Amazon Bedrock AgentCore Runtime using a pre-built
Docker image from Docker Hub (ffactory/openclaw:latest).

Deployment phases:
  Phase 1 — Foundation:  VPC, Security, Guardrails, Observability
  Phase 2 — Runtime:     AgentCore Runtime + Endpoint (via agentcore CLI)
  Phase 3 — Application: AgentCore stack, Router, Cron, Token Monitoring
"""

import os

import aws_cdk as cdk

from stacks.agentcore_stack import AgentCoreStack
from stacks.cicd_stack import CicdStack
from stacks.cron_stack import CronStack
from stacks.guardrails_stack import GuardrailsStack
from stacks.observability_stack import ObservabilityStack
from stacks.router_stack import RouterStack
from stacks.security_stack import SecurityStack
from stacks.token_monitoring_stack import TokenMonitoringStack
from stacks.vpc_stack import VpcStack

app = cdk.App()

# Account/region: prefer cdk.json context, else fall back to the ambient deploy
# credentials (CDK_DEFAULT_ACCOUNT/REGION). New accounts can omit them from
# cdk.json and they resolve from whichever profile/role is deploying.
account = app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT")
region = app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION") or "us-east-1"
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
# After Phase 2 completes, scripts/cli.py writes the runtime id to SSM
# (/openclaw/runtime-id); Phase 3 stacks read it from there, so no manual
# cdk.json edit is needed. An explicit cdk.json "runtime_id" still overrides.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 3 — Application (resolves runtime_id from cdk.json context or SSM)
# ---------------------------------------------------------------------------

agentcore_stack = AgentCoreStack(
    app,
    f"{prefix}AgentCore",
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

# ---------------------------------------------------------------------------
# CI/CD — GitHub OIDC provider + deploy roles (opt-in)
#
# Codifies the GitHub Actions deploy identity (previously hand-created IAM).
# Disabled by default so existing accounts don't clash with the manually
# created OIDC provider/roles. Enable for a NEW account by setting context
# `enable_cicd_stack=true` (and `cicd_create_oidc_provider=false` if that
# account already has a GitHub OIDC provider). See docs/NEW-ACCOUNT.md.
# ---------------------------------------------------------------------------

if app.node.try_get_context("enable_cicd_stack"):
    CicdStack(
        app,
        f"{prefix}Cicd",
        env=env,
        description="OpenClaw — GitHub OIDC provider + keyless CI/CD deploy roles",
    )

app.synth()
