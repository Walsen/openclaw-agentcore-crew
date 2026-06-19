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

## Documentation

- **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** — canonical deployment guide: build/deploy paths (CDK via GitHub Actions, image publish, runtime env), automated vs. manual steps, runtime-tunable SSM config, image-update workflow, ops.
- **[docs/NEW-ACCOUNT.md](docs/NEW-ACCOUNT.md)** — step-by-step runbook for standing up OpenClaw in a fresh AWS account (every manual step, in order).

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
just deploy-phase2             # default: build the pinned local image (needs Docker)
just deploy-phase2-dockerhub   # alt: pull ffactory/openclaw:latest from Docker Hub
```
- Builds (or pulls) the agent image and pushes to ECR
- Creates/updates the AgentCore Runtime and injects its env (incl. Google `GOG_*`)
- Writes `runtime_id` to `cdk.json` **and** SSM (`/openclaw/runtime-id`)

> Real CDK deploys (Phase 1/3) run through the GitHub Actions `cdk-deploy.yml`
> workflow (OIDC) — the devbox `cdk` CLI is intentionally older. See
> [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

### Phase 3 — Application
```bash
just deploy-phase3
```
Deploys: Router Lambda + API Gateway, Cron Lambda, Token Monitoring. Also seeds
the runtime-tunable SSM config (`/openclaw/config/*`, create-if-absent).

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

## Google Workspace Integration

Connect OpenClaw to one or more Google accounts (Gmail, Calendar, Drive, Sheets, Docs, Contacts) with a single OAuth setup per account.

**Run after Phase 1 is deployed** (the secret must exist before Phase 2 injects it):

```bash
just setup-google          # add first account
just setup-google          # run again to add a second account
just google-accounts       # list all configured accounts
just google-default you@work.com   # change the default
just google-remove you@old.com     # remove an account
```

The wizard will:
1. Walk you through creating a Google Cloud project and enabling the APIs (first time only — subsequent accounts can reuse the same OAuth client)
2. Guide you through creating an OAuth 2.0 Desktop client
3. Open a browser for the one-time authorization flow per account
4. Store all credentials in a single Secrets Manager secret (`openclaw/google-oauth`)
5. Save the default account email to `cdk.json`
6. **Re-inject the credentials into the running runtime automatically** (image-preserving, no Docker, no redeploy). Pass `--no-deploy` to skip and apply later.

> Adding a second account does not change your default. Address a non-default
> account by name ("my work email" / the address); "my email" uses the default.
> Change the default anytime with `just google-default you@work.com` (no re-auth).

**Google Workspace accounts:** for a managed domain (not personal Gmail), add the
address as a **test user** on the OAuth consent screen if the app is in "Testing"
mode, and ensure the Workspace admin allows the app (Admin console → Security →
API controls). Apps left in Testing expire refresh tokens after ~7 days — publish
the consent app for long-lived tokens.

**Scope options** (choose per account):
- **Read-only** (recommended to start) — read Gmail, Calendar, Drive; cannot send or delete
- **Full access** — read + send email, create/edit calendar events, edit Drive files

**Example things you can ask OpenClaw:**
- *"Check my work email for unread messages"*
- *"Find all invoices in my personal Gmail this month and total the amounts"*
- *"What meetings do I have on my work calendar this week?"*
- *"Search my personal Drive for the Q1 budget spreadsheet"*
- *"Send a reply from my work account to the last email from Alice"*

**To change scopes or rotate a token**, re-run `just setup-google` with the same email address (or `just refresh-google-token you@example.com <scope-level>`) — it overwrites that account's entry and re-injects automatically.

**Secret structure** (`openclaw/google-oauth`):

```json
{
  "accounts": {
    "you@gmail.com":  { "client_id", "client_secret", "refresh_token", "scopes", "label": "personal" },
    "you@work.com":   { "client_id", "client_secret", "refresh_token", "scopes", "label": "work" }
  },
  "default_account": "you@gmail.com"
}
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
| `runtime_id` | — | Set automatically after Phase 2 (also published to SSM `/openclaw/runtime-id`) |
| `google_account` | — | Set automatically by `just setup-google` |

### Runtime-tunable config (SSM, no redeploy)

A few operational knobs live in SSM `/openclaw/config/*` and are read by the
Lambdas at invocation (~60s cache), so they change **without a redeploy**. Phase 3
seeds them create-if-absent from the `cdk.json` defaults below.

| Parameter | Source default | Used by |
|---|---|---|
| `/openclaw/config/max-users` | `max_users` | Router (registration cap) |
| `/openclaw/config/registration-open` | `registration_open` | Router (self-registration) |
| `/openclaw/config/daily-token-budget` | `daily_token_budget` | Token processor |
| `/openclaw/config/daily-cost-budget-usd` | `daily_cost_budget_usd` | Token processor |

```bash
# change a value live (effective within ~a minute)
aws ssm put-parameter --overwrite --type String \
  --name /openclaw/config/registration-open --value true \
  --profile walsen --region us-east-1
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full deploy paths.

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
