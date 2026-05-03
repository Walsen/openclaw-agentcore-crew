#!/usr/bin/env bash
# setup-telegram.sh — Register Telegram webhook and add first user
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

REGION=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['region'])")
PREFIX=$(python3 -c "import json; print(json.load(open('cdk.json'))['context'].get('stack_prefix', 'OpenClaw'))")

API_URL=$(aws cloudformation describe-stacks \
    --stack-name "${PREFIX}Router" \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
    --output text --region "$REGION")

echo "OpenClaw Telegram Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━"

# Get bot token from Secrets Manager
TOKEN=$(aws secretsmanager get-secret-value \
    --secret-id openclaw/channels/telegram \
    --query SecretString --output text --region "$REGION" 2>/dev/null || echo "")

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo ""
    echo "⚠ No Telegram bot token found in Secrets Manager."
    echo "  1. Talk to @BotFather on Telegram to create a bot"
    echo "  2. Store the token:"
    echo ""
    echo "  aws secretsmanager update-secret \\"
    echo "    --secret-id openclaw/channels/telegram \\"
    echo "    --secret-string 'YOUR_BOT_TOKEN' \\"
    echo "    --region $REGION"
    echo ""
    read -rp "Enter your bot token now (or Ctrl+C to exit): " TOKEN
    aws secretsmanager update-secret \
        --secret-id openclaw/channels/telegram \
        --secret-string "$TOKEN" \
        --region "$REGION"
    echo "✓ Token stored"
fi

# Register webhook
WEBHOOK_URL="${API_URL}/webhook/telegram"
echo ""
echo "→ Registering webhook: $WEBHOOK_URL"

RESULT=$(curl -s "https://api.telegram.org/bot${TOKEN}/setWebhook?url=${WEBHOOK_URL}")
echo "  Telegram API response: $RESULT"

# Add user to allowlist
echo ""
echo "→ Add yourself to the allowlist"
echo "  Get your Telegram user ID from @userinfobot"
read -rp "  Your Telegram user ID: " TG_USER_ID

TABLE_NAME=$(aws cloudformation describe-stacks \
    --stack-name "${PREFIX}Router" \
    --query "Stacks[0].Outputs[?OutputKey=='IdentityTableName'].OutputValue" \
    --output text --region "$REGION")

aws dynamodb put-item \
    --table-name "$TABLE_NAME" \
    --item "{
        \"pk\": {\"S\": \"USER#user_telegram_${TG_USER_ID}\"},
        \"sk\": {\"S\": \"PROFILE\"},
        \"user_id\": {\"S\": \"user_telegram_${TG_USER_ID}\"},
        \"channel_id\": {\"S\": \"telegram:${TG_USER_ID}\"},
        \"display_name\": {\"S\": \"Admin\"},
        \"created_at\": {\"N\": \"$(date +%s)\"},
        \"status\": {\"S\": \"active\"}
    }" \
    --region "$REGION"

echo "✓ User telegram:${TG_USER_ID} added to allowlist"
echo ""
echo "Done! Send a message to your bot to test."
