# OpenClaw AgentCore — Justfile
# Run `just` to see available commands

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

# Synthesize CloudFormation templates (validates the CDK code)
synth:
    source .venv/bin/activate && cdk synth

# Show diff between deployed and local stacks
diff:
    source .venv/bin/activate && cdk diff

# ── Deployment ───────────────────────────────────────────────────────────

# Full 3-phase deployment
deploy:
    source .venv/bin/activate && python cli.py deploy

# Deploy Phase 1 only (VPC, Security, Guardrails, Observability)
deploy-phase1:
    source .venv/bin/activate && python cli.py deploy --phase 1

# Deploy Phase 2 only (AgentCore Runtime — pulls image from Docker Hub)
deploy-phase2:
    source .venv/bin/activate && python cli.py deploy --phase 2

# Deploy Phase 3 only (Router, Cron, Token Monitoring)
deploy-phase3:
    source .venv/bin/activate && python cli.py deploy --phase 3

# ── Channel Setup ────────────────────────────────────────────────────────

# Set up Telegram webhook and add first user
setup-telegram:
    source .venv/bin/activate && python cli.py setup telegram

# Set up Slack app credentials
setup-slack:
    source .venv/bin/activate && python cli.py setup slack

# Set up WhatsApp Business API
setup-whatsapp:
    source .venv/bin/activate && python cli.py setup whatsapp

# Set up Discord bot
setup-discord:
    source .venv/bin/activate && python cli.py setup discord

# ── User Management ──────────────────────────────────────────────────────

# List all registered users
users:
    source .venv/bin/activate && python cli.py users list

# Add a user: just add-user telegram:123456789 "Alice"
add-user channel_id display_name="User":
    source .venv/bin/activate && python cli.py users add {{channel_id}} "{{display_name}}"

# Remove a user: just remove-user telegram:123456789
remove-user channel_id:
    source .venv/bin/activate && python cli.py users remove {{channel_id}}

# ── Ops ──────────────────────────────────────────────────────────────────

# Show all stack outputs
outputs:
    source .venv/bin/activate && python cli.py outputs

# Show deployment status of all stacks
status:
    source .venv/bin/activate && python cli.py status

# List Secrets Manager secrets
secrets:
    source .venv/bin/activate && python cli.py secrets

# Tail Router Lambda logs
logs-router:
    aws logs tail /aws/lambda/openclaw-router --follow \
        --region $(jq -r '.context.region' cdk.json)

# Tail Cron Lambda logs
logs-cron:
    aws logs tail /aws/lambda/openclaw-cron --follow \
        --region $(jq -r '.context.region' cdk.json)

# ── Teardown ─────────────────────────────────────────────────────────────

# Preview what teardown would delete (safe — no changes)
teardown-dry-run:
    source .venv/bin/activate && python cli.py teardown --dry-run

# Full interactive teardown — prompts before each step
teardown:
    source .venv/bin/activate && python cli.py teardown

# Force teardown without prompts — USE WITH CAUTION
teardown-force:
    source .venv/bin/activate && python cli.py teardown --force
