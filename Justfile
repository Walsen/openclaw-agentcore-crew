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
    @echo "✓ Dependencies installed. Activate with: source .venv/bin/activate"

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
    ./scripts/deploy.sh

# Deploy Phase 1 only (VPC, Security, Guardrails, Observability)
deploy-phase1:
    ./scripts/deploy.sh --phase1

# Deploy Phase 2 only (AgentCore Runtime — pulls image from Docker Hub)
deploy-phase2:
    ./scripts/deploy.sh --phase2

# Deploy Phase 3 only (Router, Cron, Token Monitoring)
deploy-phase3:
    ./scripts/deploy.sh --phase3

# ── Channel Setup ────────────────────────────────────────────────────────

# Set up Telegram webhook and add first user
setup-telegram:
    ./scripts/setup-telegram.sh

# Set up Slack app credentials
setup-slack:
    ./scripts/setup-slack.sh

# Set up WhatsApp Business API
setup-whatsapp:
    ./scripts/setup-whatsapp.sh

# Set up Discord bot
setup-discord:
    ./scripts/setup-discord.sh

# ── User Management ──────────────────────────────────────────────────────

# List all registered users
users:
    ./scripts/manage-allowlist.sh list

# Add a user: just add-user telegram:123456789 "Alice"
add-user channel_id display_name="User":
    ./scripts/manage-allowlist.sh add {{channel_id}} "{{display_name}}"

# Remove a user: just remove-user telegram:123456789
remove-user channel_id:
    ./scripts/manage-allowlist.sh remove {{channel_id}}

# ── Ops ──────────────────────────────────────────────────────────────────

# Tail Router Lambda logs
logs-router:
    aws logs tail /aws/lambda/openclaw-router --follow \
        --region $(jq -r '.context.region' cdk.json)

# Tail Cron Lambda logs
logs-cron:
    aws logs tail /aws/lambda/openclaw-cron --follow \
        --region $(jq -r '.context.region' cdk.json)

# Show all stack outputs
outputs:
    #!/usr/bin/env bash
    REGION=$(jq -r '.context.region' cdk.json)
    PREFIX=$(jq -r '.context.stack_prefix // "OpenClaw"' cdk.json)
    for stack in Vpc Security Guardrails Observability AgentCore Router Cron TokenMonitoring; do
        echo "━━━ ${PREFIX}${stack} ━━━"
        aws cloudformation describe-stacks \
            --stack-name "${PREFIX}${stack}" \
            --query "Stacks[0].Outputs" \
            --output table \
            --region "$REGION" 2>/dev/null || echo "  (not deployed)"
    done

# Destroy all stacks (DESTRUCTIVE — prompts for confirmation)
destroy:
    source .venv/bin/activate && cdk destroy --all
