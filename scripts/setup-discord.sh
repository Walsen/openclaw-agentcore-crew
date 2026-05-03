#!/usr/bin/env bash
# setup-discord.sh — Configure Discord bot and interaction endpoint
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

echo "OpenClaw Discord Setup"
echo "━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "1. Go to https://discord.com/developers/applications"
echo "2. Create a new application"
echo "3. Under 'Bot', create a bot and copy the token"
echo "4. Under 'General Information', copy the Application ID and Public Key"
echo ""

read -rp "Bot Token: " BOT_TOKEN
read -rp "Application ID: " APP_ID
read -rp "Public Key: " PUBLIC_KEY

SECRET_JSON="{\"botToken\":\"${BOT_TOKEN}\",\"applicationId\":\"${APP_ID}\",\"publicKey\":\"${PUBLIC_KEY}\"}"
aws secretsmanager update-secret \
    --secret-id openclaw/channels/discord \
    --secret-string "$SECRET_JSON" \
    --region "$REGION"
echo "✓ Discord credentials stored"

echo ""
echo "5. Under 'General Information', set Interactions Endpoint URL to:"
echo "   ${API_URL}/webhook/discord"
echo ""
echo "6. Under 'OAuth2 → URL Generator':"
echo "   Scopes: bot, applications.commands"
echo "   Bot Permissions: Send Messages, Read Message History"
echo "   Copy the generated URL and open it to invite the bot"
echo ""
echo "Done! Use slash commands or DM the bot to test."
