# OpenClaw AgentCore Infrastructure

Deploy [OpenClaw](https://github.com/Walsen/openclaw) on **Amazon Bedrock AgentCore Runtime** — serverless, per-user microVMs with Telegram, Slack, WhatsApp, and Discord support.

## Architecture

```
Telegram / Slack / WhatsApp / Discord
              │
              ▼
      API Gateway (HTTP API)
              │
              ▼
        Router Lambda
    - validates webhook signatures
    - resolves user identity (DynamoDB)
    - calls InvokeAgentRuntime
              │
              ▼
    AgentCore Runtime  ◄── ffactory/openclaw:latest (ARM64)
    - per-user Firecracker microVM
    - OpenClaw Gateway (port 18789)
    - server.py HTTP contract (port 8080)
    - S3 workspace sync
              │
              ▼
    Amazon Bedrock (Claude via ConverseStream)
```

## Prerequisites

This project uses [devbox](https://www.jetify.com/devbox) for a reproducible dev environment. All tools (Python 3.14, uv, Node.js, AWS CDK, AWS CLI, just, jq, yq) are managed by devbox.

```bash
# Install devbox (if not already installed)
curl -fsSL https://get.jetify.com/devbox | bash

# Enter the devbox shell — all tools are available
devbox shell

# Docker is required for Phase 2 (pushing image to ECR)
# Install separately: https://docs.docker.com/get-docker/

# AgentCore CLI (install inside devbox shell)
uv tool install bedrock-agentcore-toolkit
```

## Quick Start

```bash
# Enter devbox shell
devbox shell

# 1. Configure AWS credentials
aws configure
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
```

```bash
# 2. Edit cdk.json — set your account ID
#    "account": "123456789012"

# 3. Install Python dependencies
just install

# 4. Bootstrap CDK (first time only)
source .venv/bin/activate && cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/us-east-1

# 5. Validate CDK
just synth

# 6. Deploy everything
just deploy

# Or deploy phase by phase:
just deploy-phase1   # VPC, Security, Guardrails, Observability
just deploy-phase2   # AgentCore Runtime (pulls image from Docker Hub)
just deploy-phase3   # Router, Cron, Token Monitoring
```

```bash
# 7. Set up channels
just setup-telegram
just setup-slack
just setup-whatsapp
just setup-discord

# 8. Manage users
just add-user telegram:123456789 "Alice"
just add-user slack:U0123ABCD "Bob"
just users
```

## CDK Stacks

| Phase | Stack | Resources |
|-------|-------|-----------|
| 1 | `OpenClawVpc` | VPC, 2 AZs, NAT, VPC endpoints |
| 1 | `OpenClawSecurity` | KMS CMK, Secrets Manager (per channel), Cognito |
| 1 | `OpenClawGuardrails` | Bedrock Guardrail (content, PII, topics) |
| 1 | `OpenClawObservability` | CloudWatch dashboard, alarms, SNS |
| 2 | *(agentcore CLI)* | ECR repo, AgentCore Runtime + Endpoint |
| 3 | `OpenClawAgentCore` | IAM role, S3 workspace bucket, security group |
| 3 | `OpenClawRouter` | Router Lambda, API Gateway, DynamoDB identity |
| 3 | `OpenClawCron` | EventBridge Scheduler, Cron Lambda |
| 3 | `OpenClawTokenMonitoring` | DynamoDB usage table, processor Lambda |

## Configuration

All settings are in `cdk.json` context. Key values:

| Setting | Default | Description |
|---------|---------|-------------|
| `docker_image` | `ffactory/openclaw:latest` | Docker Hub image |
| `default_model_id` | `us.anthropic.claude-sonnet-4-*` | Bedrock model |
| `session_idle_timeout` | `1800` | Idle timeout (seconds) |
| `enable_guardrails` | `true` | Content safety filters |
| `registration_open` | `false` | Allow self-registration |
| `max_users` | `10` | Maximum registered users |
| `daily_token_budget` | `1000000` | Daily token limit |
| `daily_cost_budget_usd` | `10` | Daily cost limit |

## Image Updates

When `ffactory/openclaw` publishes a new image:

```bash
# Re-run Phase 2 to pull and push the latest image
just deploy-phase2
```

## Ops

```bash
just logs-router    # tail Router Lambda logs
just logs-cron      # tail Cron Lambda logs
just outputs        # show all stack outputs
just diff           # diff deployed vs local
```

## Teardown

```bash
# Preview what will be deleted (safe — no changes)
just teardown-dry-run

# Full interactive teardown — prompts before each step
just teardown

# Force teardown without prompts (CI/CD use only)
just teardown-force
```

The teardown script handles everything `cdk destroy` misses:

| Resource | How removed |
|----------|-------------|
| AgentCore Runtime + Endpoint | `aws bedrock-agentcore delete-*` |
| CDK stacks (Phase 3 then 1) | `cdk destroy` per stack |
| S3 workspace bucket | Emptied (all versions) then deleted |
| ECR repository | `aws ecr delete-repository --force` |
| KMS key | Scheduled for deletion (7-day window) |
| CloudWatch log groups | `aws logs delete-log-group` |
| Telegram webhook | `deleteWebhook` API call |

**Not removed automatically** (contain sensitive data — delete manually):
- Secrets Manager secrets (`openclaw/channels/*`) — hold your bot tokens
- DynamoDB identity table (`openclaw-identity`) — holds user data
- CDK bootstrap stack (`CDKToolkit`) — shared, keep if reusing the account
