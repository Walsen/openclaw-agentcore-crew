#!/usr/bin/env bash
# setup-slack.sh — Configure Slack app and store credentials
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

echo "OpenClaw Slack Setup"
echo "━━━━━━━━━━━━━━━━━━━"
echo ""
echo "1. Go to https://api.slack.com/apps and create a new app"
echo "2. Under 'OAuth & Permissions', add these Bot Token Scopes:"
echo "   - chat:write"
echo "   - app_mentions:read"
echo "   - im:history"
echo "   - im:read"
echo "   - im:write"
echo "3. Install the app to your workspace"
echo "4. Copy the Bot User OAuth Token (xoxb-...)"
echo "5. Copy the Signing Secret from 'Basic Information'"
echo ""

read -rp "Bot Token (xoxb-...): " BOT_TOKEN
read -rp "Signing Secret: " SIGNING_SECRET

# Store in Secrets Manager
SECRET_JSON="{\"botToken\":\"${BOT_TOKEN}\",\"signingSecret\":\"${SIGNING_SECRET}\"}"
aws secretsmanager update-secret \
    --secret-id openclaw/channels/slack \
    --secret-string "$SECRET_JSON" \
    --region "$REGION"
echo "✓ Slack credentials stored"

echo ""
echo "6. Under 'Event Subscriptions', enable events and set Request URL to:"
echo "   ${API_URL}/webhook/slack"
echo ""
echo "7. Subscribe to bot events: message.im, app_mention"
echo ""
echo "Done! The bot will respond to DMs and @mentions."
