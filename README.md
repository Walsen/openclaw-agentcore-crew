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

# AWS SSO — log in before running any scripts
aws sso login --profile YOUR_SSO_PROFILE_NAME
```

## Step-by-Step Deployment Guide

### Before you start — one-time setup

```bash
# 1. Enter the devbox shell (installs all tools automatically)
devbox shell

# 2. Log in with SSO
aws sso login --profile YOUR_SSO_PROFILE_NAME
#    This opens a browser window — approve the login

# 3. Verify you're connected to the right account
aws sts get-caller-identity --profile YOUR_SSO_PROFILE_NAME
#    You should see your Account ID, UserId, and Arn

# 4. Edit cdk.json — set your account ID and SSO profile name
#    "account": "123456789012"
#    "aws_profile": "YOUR_SSO_PROFILE_NAME"

# 5. Install Python dependencies (includes boto3, rich, aws-cdk-lib)
just install

# 6. Bootstrap CDK (only needed once per AWS account/region)
source .venv/bin/activate
AWS_PROFILE=YOUR_SSO_PROFILE_NAME cdk bootstrap aws://YOUR_ACCOUNT_ID/us-east-1

# 7. Check status — verifies credentials and shows what's deployed
just status
```

---

### Phase 1 — Foundation (~10 min, no manual steps)

Deploys the AWS networking and security infrastructure. Nothing to configure — just run it.

```bash
just deploy-phase1
```

**What gets created:**
- `OpenClawVpc` — VPC with 2 availability zones, private/public subnets, NAT gateway
- `OpenClawSecurity` — KMS encryption key, empty Secrets Manager secrets for each channel
- `OpenClawGuardrails` — Bedrock content safety filters (blocks harmful content, redacts PII)
- `OpenClawObservability` — CloudWatch dashboard and SNS alarm topic

**Phase 1 ends when:** the terminal prints `✓ Phase 1 complete` and all 4 stacks show `CREATE_COMPLETE` in the AWS Console under CloudFormation.

---

### Phase 2 — AgentCore Runtime (~15 min, one manual step after)

Pulls `ffactory/openclaw:latest` from Docker Hub, pushes it to your ECR, and registers it as an AgentCore Runtime. **Docker must be running on your machine.**

```bash
just deploy-phase2
```

**What gets created:**
- ECR repository `openclaw-runtime` in your account
- The Docker image pushed to that ECR repo
- An AgentCore Runtime (the serverless microVM environment)

**⚠ Manual step after Phase 2 completes:**

The CLI will print something like:
```
Runtime created: openclaw_agent-a1b2c3d4e5
```

Copy that ID and add it to `cdk.json`:
```json
"runtime_id": "openclaw_agent-a1b2c3d4e5"
```

**Phase 2 ends when:** you've added `runtime_id` to `cdk.json`.

---

### Phase 3 — Application (~8 min, no manual steps)

Deploys the Lambda functions, API Gateway, and DynamoDB tables that connect your messaging channels to the AgentCore Runtime.

```bash
just deploy-phase3
```

**What gets created:**
- `OpenClawAgentCore` — IAM role, S3 bucket for user workspaces, security group
- `OpenClawRouter` — Router Lambda + API Gateway (this is your webhook URL), DynamoDB identity table
- `OpenClawCron` — EventBridge Scheduler + Cron Lambda (for scheduled reminders)
- `OpenClawTokenMonitoring` — Usage tracking and daily budget enforcement

**Phase 3 ends when:** the terminal prints your API URL:
```
API URL: https://abc123.execute-api.us-east-1.amazonaws.com
```

Save that URL — you'll need it for the channel setup steps.

---

### Channel Setup — connect your messaging apps

Run the setup script for each channel you want to use. Each script walks you through the steps interactively.

```bash
just setup-telegram   # needs a bot token from @BotFather
just setup-slack      # needs a Slack app with bot token + signing secret
just setup-whatsapp   # needs a Meta Business account + WhatsApp API token
just setup-discord    # needs a Discord application + bot token + public key
```

Each script will:
1. Ask for your credentials
2. Store them in Secrets Manager (encrypted with your KMS key)
3. Register the webhook URL with the platform
4. Add you to the user allowlist

---

### Add more users

```bash
# Add a user by their platform ID
just add-user telegram:123456789 "Alice"
just add-user slack:U0123ABCD "Bob"
just add-user whatsapp:15551234567 "Carol"
just add-user discord:987654321098 "Dave"

# See all registered users
just users
```

Get Telegram user IDs from [@userinfobot](https://t.me/userinfobot).
Get Slack user IDs from the Slack member profile URL or the API.

---

### Verify it's working

Send a message to your bot on any connected channel. The first message triggers a cold start (~10-15 seconds). Subsequent messages in the same session are fast.

```bash
# Watch the Router Lambda logs in real time
just logs-router

# See all deployed stack outputs (URLs, table names, etc.)
just outputs
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
