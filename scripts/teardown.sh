#!/usr/bin/env bash
# teardown.sh — Full cleanup of all OpenClaw AWS resources
#
# Usage:
#   ./scripts/teardown.sh              # Interactive — confirms before each step
#   ./scripts/teardown.sh --force      # Skip confirmations (CI/CD use only)
#   ./scripts/teardown.sh --dry-run    # Print what would be deleted, do nothing
#
# What this script removes:
#   1. AgentCore Runtime + Endpoint  (deployed via agentcore CLI, not CDK)
#   2. CDK Phase 3 stacks            (Router, Cron, TokenMonitoring, AgentCore)
#   3. CDK Phase 1 stacks            (Observability, Guardrails, Security, Vpc)
#   4. S3 workspace bucket           (RETAIN policy — emptied then deleted)
#   5. ECR repository                (images pushed during Phase 2)
#   6. KMS key                       (RETAIN policy — scheduled for deletion)
#   7. CloudWatch Log Groups         (not removed by CDK by default)
#   8. Telegram webhook deregistration (optional)
#
# Resources NOT removed by this script:
#   - Your AWS account / IAM users
#   - Secrets Manager secrets (contain your bot tokens — delete manually)
#   - DynamoDB tables with RETAIN policy (identity table — delete manually)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# ── Config ───────────────────────────────────────────────────────────────
REGION=$(jq -r '.context.region' cdk.json)
PREFIX=$(jq -r '.context.stack_prefix // "OpenClaw"' cdk.json)
FORCE=false
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --force)   FORCE=true ;;
        --dry-run) DRY_RUN=true ;;
    esac
done

# ── Helpers ──────────────────────────────────────────────────────────────
run() {
    if $DRY_RUN; then
        echo "  [dry-run] $*"
    else
        "$@"
    fi
}

confirm() {
    local msg="$1"
    if $FORCE || $DRY_RUN; then
        echo "  → $msg"
        return 0
    fi
    echo ""
    read -rp "  ⚠ $msg [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]]
}

stack_exists() {
    aws cloudformation describe-stacks \
        --stack-name "$1" \
        --region "$REGION" &>/dev/null
}

# ── Banner ───────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  OpenClaw — Full Teardown                        ║"
if $DRY_RUN; then
echo "║  MODE: DRY RUN — nothing will be deleted         ║"
fi
echo "║  Region:  $REGION"
echo "║  Prefix:  $PREFIX"
echo "╚══════════════════════════════════════════════════╝"
echo ""

if ! $FORCE && ! $DRY_RUN; then
    echo "  This will permanently delete all OpenClaw AWS resources."
    read -rp "  Type 'delete' to confirm: " confirm_word
    if [[ "$confirm_word" != "delete" ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# ── Step 1: AgentCore Runtime (deployed via CLI, not CDK) ────────────────
echo ""
echo "━━━ Step 1: AgentCore Runtime ━━━"

RUNTIME_ID=$(jq -r '.context.runtime_id // ""' cdk.json 2>/dev/null || echo "")

if [ -n "$RUNTIME_ID" ] && [ "$RUNTIME_ID" != "null" ]; then
    if confirm "Delete AgentCore Runtime: $RUNTIME_ID"; then
        # Delete endpoint first
        ENDPOINT_ID=$(aws bedrock-agentcore list-agent-runtime-endpoints \
            --agent-runtime-id "$RUNTIME_ID" \
            --query "agentRuntimeEndpoints[0].agentRuntimeEndpointId" \
            --output text --region "$REGION" 2>/dev/null || echo "")

        if [ -n "$ENDPOINT_ID" ] && [ "$ENDPOINT_ID" != "None" ]; then
            echo "  → Deleting endpoint: $ENDPOINT_ID"
            run aws bedrock-agentcore delete-agent-runtime-endpoint \
                --agent-runtime-id "$RUNTIME_ID" \
                --endpoint-id "$ENDPOINT_ID" \
                --region "$REGION"
            echo "  → Waiting for endpoint deletion..."
            $DRY_RUN || sleep 10
        fi

        echo "  → Deleting runtime: $RUNTIME_ID"
        run aws bedrock-agentcore delete-agent-runtime \
            --agent-runtime-id "$RUNTIME_ID" \
            --region "$REGION"
        echo "  ✓ AgentCore Runtime deleted"
    fi
else
    echo "  No runtime_id in cdk.json — skipping (may not have been deployed)"
fi

# ── Step 2: CDK Phase 3 stacks ───────────────────────────────────────────
echo ""
echo "━━━ Step 2: CDK Phase 3 Stacks ━━━"

PHASE3_STACKS=(
    "${PREFIX}TokenMonitoring"
    "${PREFIX}Cron"
    "${PREFIX}Router"
    "${PREFIX}AgentCore"
)

if confirm "Destroy CDK Phase 3 stacks: ${PHASE3_STACKS[*]}"; then
    source .venv/bin/activate
    for stack in "${PHASE3_STACKS[@]}"; do
        if stack_exists "$stack"; then
            echo "  → Destroying $stack..."
            run cdk destroy "$stack" --force
        else
            echo "  → $stack not found, skipping"
        fi
    done
    echo "  ✓ Phase 3 stacks destroyed"
fi

# ── Step 3: CDK Phase 1 stacks ───────────────────────────────────────────
echo ""
echo "━━━ Step 3: CDK Phase 1 Stacks ━━━"

PHASE1_STACKS=(
    "${PREFIX}Observability"
    "${PREFIX}Guardrails"
    "${PREFIX}Security"
    "${PREFIX}Vpc"
)

if confirm "Destroy CDK Phase 1 stacks: ${PHASE1_STACKS[*]}"; then
    source .venv/bin/activate
    for stack in "${PHASE1_STACKS[@]}"; do
        if stack_exists "$stack"; then
            echo "  → Destroying $stack..."
            run cdk destroy "$stack" --force
        else
            echo "  → $stack not found, skipping"
        fi
    done
    echo "  ✓ Phase 1 stacks destroyed"
fi

# ── Step 4: S3 workspace bucket (RETAIN policy) ──────────────────────────
echo ""
echo "━━━ Step 4: S3 Workspace Bucket ━━━"

BUCKET_NAME="${PREFIX,,}-workspaces-$(aws sts get-caller-identity --query Account --output text)-${REGION}"

if aws s3api head-bucket --bucket "$BUCKET_NAME" --region "$REGION" 2>/dev/null; then
    if confirm "Empty and delete S3 bucket: $BUCKET_NAME (ALL USER WORKSPACES WILL BE LOST)"; then
        echo "  → Removing all object versions..."
        run aws s3api delete-objects \
            --bucket "$BUCKET_NAME" \
            --delete "$(aws s3api list-object-versions \
                --bucket "$BUCKET_NAME" \
                --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' \
                --output json 2>/dev/null)" \
            --region "$REGION" 2>/dev/null || true

        echo "  → Removing delete markers..."
        run aws s3api delete-objects \
            --bucket "$BUCKET_NAME" \
            --delete "$(aws s3api list-object-versions \
                --bucket "$BUCKET_NAME" \
                --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' \
                --output json 2>/dev/null)" \
            --region "$REGION" 2>/dev/null || true

        echo "  → Deleting bucket..."
        run aws s3 rb "s3://$BUCKET_NAME" --force --region "$REGION"
        echo "  ✓ S3 bucket deleted"
    fi
else
    echo "  Bucket $BUCKET_NAME not found — skipping"
fi

# ── Step 5: ECR repository ───────────────────────────────────────────────
echo ""
echo "━━━ Step 5: ECR Repository ━━━"

if aws ecr describe-repositories --repository-names openclaw-runtime --region "$REGION" 2>/dev/null; then
    if confirm "Delete ECR repository: openclaw-runtime (all images will be lost)"; then
        run aws ecr delete-repository \
            --repository-name openclaw-runtime \
            --force \
            --region "$REGION"
        echo "  ✓ ECR repository deleted"
    fi
else
    echo "  ECR repository not found — skipping"
fi

# ── Step 6: KMS key (RETAIN policy — schedule for deletion) ─────────────
echo ""
echo "━━━ Step 6: KMS Key ━━━"

KMS_KEY_ID=$(aws kms describe-key \
    --key-id "alias/openclaw/master" \
    --query "KeyMetadata.KeyId" \
    --output text --region "$REGION" 2>/dev/null || echo "")

if [ -n "$KMS_KEY_ID" ] && [ "$KMS_KEY_ID" != "None" ]; then
    if confirm "Schedule KMS key for deletion (7-day waiting period): $KMS_KEY_ID"; then
        run aws kms schedule-key-deletion \
            --key-id "$KMS_KEY_ID" \
            --pending-window-in-days 7 \
            --region "$REGION"
        echo "  ✓ KMS key scheduled for deletion in 7 days"
        echo "  ℹ To cancel: aws kms cancel-key-deletion --key-id $KMS_KEY_ID --region $REGION"
    fi
else
    echo "  KMS key not found — skipping"
fi

# ── Step 7: CloudWatch Log Groups ────────────────────────────────────────
echo ""
echo "━━━ Step 7: CloudWatch Log Groups ━━━"

LOG_GROUPS=$(aws logs describe-log-groups \
    --log-group-name-prefix "/aws/lambda/openclaw" \
    --query "logGroups[].logGroupName" \
    --output text --region "$REGION" 2>/dev/null || echo "")

if [ -n "$LOG_GROUPS" ]; then
    if confirm "Delete CloudWatch log groups: $LOG_GROUPS"; then
        for lg in $LOG_GROUPS; do
            echo "  → Deleting $lg"
            run aws logs delete-log-group --log-group-name "$lg" --region "$REGION"
        done
        echo "  ✓ Log groups deleted"
    fi
else
    echo "  No log groups found — skipping"
fi

# ── Step 8: Deregister Telegram webhook (optional) ───────────────────────
echo ""
echo "━━━ Step 8: Telegram Webhook (optional) ━━━"

TG_SECRET=$(aws secretsmanager get-secret-value \
    --secret-id openclaw/channels/telegram \
    --query SecretString --output text --region "$REGION" 2>/dev/null || echo "")

if [ -n "$TG_SECRET" ] && [ "$TG_SECRET" != "null" ]; then
    if confirm "Deregister Telegram webhook"; then
        $DRY_RUN || curl -s "https://api.telegram.org/bot${TG_SECRET}/deleteWebhook" > /dev/null
        echo "  ✓ Telegram webhook deregistered"
    fi
else
    echo "  No Telegram token found — skipping"
fi

# ── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Teardown Complete                               ║"
echo "╠══════════════════════════════════════════════════╣"
echo "║  Resources NOT deleted (manual cleanup needed):  ║"
echo "║  • Secrets Manager secrets (contain bot tokens)  ║"
echo "║    aws secretsmanager list-secrets --region $REGION"
echo "║  • DynamoDB identity table (user data)           ║"
echo "║    openclaw-identity                             ║"
echo "║  • CDK bootstrap stack (shared, keep if reusing) ║"
echo "║    CDKToolkit                                    ║"
echo "╚══════════════════════════════════════════════════╝"
