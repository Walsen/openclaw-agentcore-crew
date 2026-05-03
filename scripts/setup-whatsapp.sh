#!/usr/bin/env bash
# setup-whatsapp.sh — Configure WhatsApp Business API webhook
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

echo "OpenClaw WhatsApp Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Prerequisites:"
echo "  - Meta Business account"
echo "  - WhatsApp Business API access"
echo ""
echo "1. Go to https://developers.facebook.com/apps/"
echo "2. Create or select your app → WhatsApp → API Setup"
echo "3. Copy the Permanent Token and Phone Number ID"
echo ""

read -rp "WhatsApp API Token: " WA_TOKEN
read -rp "Phone Number ID: " PHONE_ID
read -rp "Webhook Verify Token (choose any string): " VERIFY_TOKEN

SECRET_JSON="{\"token\":\"${WA_TOKEN}\",\"phoneNumberId\":\"${PHONE_ID}\",\"verifyToken\":\"${VERIFY_TOKEN}\"}"
aws secretsmanager update-secret \
    --secret-id openclaw/channels/whatsapp \
    --secret-string "$SECRET_JSON" \
    --region "$REGION"
echo "✓ WhatsApp credentials stored"

echo ""
echo "4. In Meta Developer Console → Webhooks → Configure:"
echo "   Callback URL: ${API_URL}/webhook/whatsapp"
echo "   Verify Token: ${VERIFY_TOKEN}"
echo "   Subscribe to: messages"
echo ""
echo "Done! Send a message to your WhatsApp Business number to test."
