## OpenClaw AgentCore — Justfile
## Run `just` to see available commands

_cli := ".venv/bin/python scripts/cli.py"

# Default: list recipes
default:
    @just --list

# ── Setup ────────────────────────────────────────────────────────────────

# Create venv and install Python dependencies
install:
    uv venv --python python3
    uv pip install -r requirements.txt
    @echo "✓ Dependencies installed"

# ── CDK ──────────────────────────────────────────────────────────────────

# Bootstrap CDK in the target account/region (run once per account)
bootstrap:
    JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 AWS_PROFILE=$(jq -r '.context.aws_profile' cdk.json) \
    VIRTUAL_ENV="$(pwd)/.venv" PATH="$(pwd)/.venv/bin:$PATH" \
    cdk bootstrap aws://$(jq -r '.context.account' cdk.json)/$(jq -r '.context.region' cdk.json)

# Synthesize CloudFormation templates (validates the CDK code)
synth:
    JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 AWS_PROFILE=$(jq -r '.context.aws_profile' cdk.json) \
    VIRTUAL_ENV="$(pwd)/.venv" PATH="$(pwd)/.venv/bin:$PATH" cdk synth --all --quiet

# Show diff between deployed and local stacks
diff:
    JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 AWS_PROFILE=$(jq -r '.context.aws_profile' cdk.json) \
    VIRTUAL_ENV="$(pwd)/.venv" PATH="$(pwd)/.venv/bin:$PATH" cdk diff

# ── Deployment ───────────────────────────────────────────────────────────

# Full 3-phase deployment (pulls image from Docker Hub)
deploy:
    {{_cli}} deploy

# Deploy Phase 1 only (VPC, Security, Guardrails, Observability)
deploy-phase1:
    {{_cli}} deploy --phase 1

# Deploy Phase 2 only — pulls ffactory/openclaw:latest from Docker Hub
deploy-phase2:
    {{_cli}} deploy --phase 2

# Deploy Phase 2 using local openclaw/ source (for development/testing)
deploy-phase2-local:
    {{_cli}} deploy --phase 2 --local

# Deploy Phase 3 only (Router, Cron, Token Monitoring)
deploy-phase3:
    {{_cli}} deploy --phase 3

# ── Image Management ─────────────────────────────────────────────────────

# Build openclaw image from local source and push to ECR (for image updates)
build-image:
    {{_cli}} build-image --source local

# Pull latest image from Docker Hub and push to ECR
build-image-dockerhub:
    {{_cli}} build-image --source dockerhub

# ── Channel Setup ────────────────────────────────────────────────────────

# Set up Telegram webhook and add first user
setup-telegram:
    {{_cli}} setup telegram

# Set up Slack app credentials
setup-slack:
    {{_cli}} setup slack

# Set up WhatsApp Business API
setup-whatsapp:
    {{_cli}} setup whatsapp

# Set up Discord bot
setup-discord:
    {{_cli}} setup discord

# ── User Management ──────────────────────────────────────────────────────

# List all registered users
users:
    {{_cli}} users list

# Alias for users
users-list:
    {{_cli}} users list

# Add a user: just add-user telegram:123456789 "Alice"
add-user channel_id display_name="User":
    {{_cli}} users add {{channel_id}} --name "{{display_name}}"

# Remove a user: just remove-user telegram:123456789
remove-user channel_id:
    {{_cli}} users remove {{channel_id}}

# ── Ops ──────────────────────────────────────────────────────────────────

# Show all CloudFormation stack outputs
outputs:
    {{_cli}} outputs

# Show deployment status of all stacks + AgentCore Runtime
status:
    {{_cli}} status

# Tail Router Lambda logs (live)
logs-router:
    {{_cli}} logs router --follow

# Tail Cron Lambda logs (live)
logs-cron:
    {{_cli}} logs cron --follow

# ── Teardown ─────────────────────────────────────────────────────────────

# Preview what teardown would delete (safe — no changes)
teardown-dry-run:
    {{_cli}} teardown --dry-run

# Full interactive teardown — prompts before each step
teardown:
    {{_cli}} teardown

# Force teardown without prompts — USE WITH CAUTION
teardown-force:
    {{_cli}} teardown --force
