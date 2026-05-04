# OpenClaw AgentCore Crew

Infrastructure-as-code for deploying [OpenClaw](https://github.com/Walsen/openclaw) on [Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-agentcore.html).

Each user gets their own serverless Firecracker microVM that spins up on demand, handles long-running AI sessions (up to 8 hours), and shuts down when idle.

## Architecture

```
Telegram / Slack / WhatsApp / Discord
         │
         ▼
   API Gateway (HTTP)
         │
         ▼
   Router Lambda          ← validates webhook, resolves user identity
         │
         ▼
  AgentCore Runtime       ← per-user Firecracker microVM (ARM64)
         │
         ▼
  openclaw agent CLI      ← runs inside the microVM
         │
         ▼
  Amazon Bedrock          ← Claude Sonnet 4 (cross-region inference)
```

**Key properties:**
- Serverless — no always-on EC2 or Fargate, pay only when chatting
- Per-user isolation — each user gets their own microVM and workspace
- Long sessions — up to 8 hours per session (vs 15 min Lambda limit)
- Multi-channel — Telegram, Slack, WhatsApp, Discord from one deployment

## Prerequisites

- AWS account with Bedrock model access enabled for `us.anthropic.claude-sonnet-4-6`
- AWS SSO profile configured (this repo uses `walsen` — change in `cdk.json`)
- [devbox](https://www.jetify.com/devbox) for the development environment
- Docker with ARM64 support (QEMU or Apple Silicon)

## Quick Start

```bash
# 1. Enter the devbox environment
cd openclaw-agentcore-crew
devbox shell

# 2. Install Python dependencies
uv pip install -r requirements.txt

# 3. Configure your settings
#    Edit cdk.json — set account, region, aws_profile

# 4. Bootstrap CDK (first time only)
just bootstrap

# 5. Deploy everything
just deploy
```

`just deploy` runs all three phases automatically.

## Deployment Phases

### Phase 1 — Foundation
```bash
just deploy-phase1
```
Deploys: VPC, Security (KMS + Secrets), Guardrails, Observability

### Phase 2 — AgentCore Runtime
```bash
just deploy-phase2
```
- Pulls `ffactory/openclaw:latest` from Docker Hub
- Pushes to ECR
- Creates/updates the AgentCore Runtime
- Saves `runtime_id` to `cdk.json` automatically

### Phase 3 — Application
```bash
just deploy-phase3
```
Deploys: Router Lambda + API Gateway, Cron Lambda, Token Monitoring

## Channel Setup

After Phase 3, set up your messaging channels:

```bash
# Telegram (requires a bot from @BotFather)
just setup-telegram

# Slack
just setup-slack

# WhatsApp (requires Meta Developer account)
just setup-whatsapp

# Discord
just setup-discord
```

## User Management

```bash
# Add a user (get numeric ID from @userinfobot on Telegram)
just add-user telegram:123456789 "Alice"

# List users
just users-list

# Remove a user
just remove-user telegram:123456789
```

## Configuration (`cdk.json`)

| Key | Default | Description |
|-----|---------|-------------|
| `account` | — | AWS account ID |
| `region` | `us-east-1` | AWS region |
| `aws_profile` | `walsen` | AWS SSO profile name |
| `docker_image` | `ffactory/openclaw:latest` | Source image from Docker Hub |
| `default_model_id` | `us.anthropic.claude-sonnet-4-6` | Bedrock model (cross-region inference profile) |
| `session_idle_timeout` | `1800` | Seconds before idle microVM shuts down |
| `session_max_lifetime` | `28800` | Max session duration (8 hours) |
| `max_users` | `10` | Maximum allowed users |
| `registration_open` | `false` | Allow self-registration |
| `channels` | `["telegram","slack","whatsapp","discord"]` | Enabled channels |
| `enable_guardrails` | `true` | Bedrock content guardrails |
| `runtime_id` | — | Set automatically after Phase 2 |

## Customizing the Agent Identity

The agent's personality is defined by `SOUL.md` in each user's S3 workspace. Upload a custom one:

```bash
cat > SOUL.md << 'EOF'
# My Assistant

You are a helpful AI assistant for the Smith family.
You help with scheduling, research, and daily tasks.
You are friendly, concise, and proactive.
EOF

AWS_PROFILE=walsen aws s3 cp SOUL.md \
  s3://openclaw-workspaces-ACCOUNT-REGION/USER_ID/workspace/SOUL.md
```

Replace `ACCOUNT`, `REGION`, and `USER_ID` with your values. `USER_ID` is the base tenant ID (e.g. `14217009` for Telegram user `14217009`).

## Teardown

```bash
# Full teardown (interactive, asks for confirmation)
just teardown

# Dry run — shows what would be deleted
just teardown-dry-run
```

Resources NOT deleted automatically (manual cleanup):
- Secrets Manager secrets (contain bot tokens)
- DynamoDB identity table
- CDK bootstrap stack

## Status & Logs

```bash
# Check stack status
just status

# View router Lambda logs (live)
just logs-router

# View cron Lambda logs
just logs-cron

# Show all CloudFormation outputs
just outputs
```

## Deployed Resources

| Resource | Name/ID |
|----------|---------|
| AgentCore Runtime | `openclaw_agent-mF4Hq3HJz8` |
| API Gateway | `https://qreftvsc4b.execute-api.us-east-1.amazonaws.com` |
| ECR Repository | `openclaw-runtime` |
| S3 Workspace Bucket | `openclaw-workspaces-862307432587-us-east-1` |
| DynamoDB Identity Table | `openclaw-identity` |
| KMS Key | `alias/openclaw/master` |
| Guardrail ID | `yvd0hs5ait8n` |

## Troubleshooting

**Bot not responding:**
```bash
just logs-router  # check for errors
```

**Health check failures:**
- Ensure the AgentCore Runtime is in PUBLIC network mode (not VPC)
- Check execution role has ECR pull permissions

**Model access errors:**
- Verify `us.anthropic.claude-sonnet-4-6` is enabled in Bedrock console
- Check the model isn't marked as Legacy in your account

**SSO credentials expired:**
```bash
rm -rf ~/.aws/sso/cache/*
aws sso login --profile walsen
```

**Redeploy after image update:**
```bash
just deploy-phase2  # pulls new image, builds wrapper, updates runtime
```

## How the Wrapper Works

The deployment builds a thin wrapper on top of `ffactory/openclaw:latest` that:

1. Replaces the entrypoint with `agentcore_start.py` — a clean Python startup script
2. Pre-injects AWS credentials at container startup (before any request arrives)
3. Writes `openclaw.json` with the correct region and model ID
4. Starts `server.py` directly, bypassing the complex `entrypoint.sh`

This is necessary because the Node.js openclaw CLI needs explicit `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars — it doesn't always pick up container credentials from the IAM role automatically.

## Cost Estimate (10 users, moderate usage)

| Resource | Monthly Cost |
|----------|-------------|
| AgentCore Runtime (serverless) | ~$0.10/hr active |
| API Gateway | ~$1/million requests |
| Lambda (Router + Cron) | ~$0 (free tier) |
| S3 (workspaces) | ~$1 |
| Bedrock (Claude Sonnet 4) | ~$3/1M tokens |
| Guardrails | ~$0.75/1k text units |
| **Total** | **~$5-20/month** depending on usage |
