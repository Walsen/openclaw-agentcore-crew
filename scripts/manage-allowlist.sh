#!/usr/bin/env bash
# manage-allowlist.sh — Add or remove users from the OpenClaw allowlist
#
# Usage:
#   ./scripts/manage-allowlist.sh add telegram:123456789 "Alice"
#   ./scripts/manage-allowlist.sh add slack:U0123ABCD "Bob"
#   ./scripts/manage-allowlist.sh remove telegram:123456789
#   ./scripts/manage-allowlist.sh list
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

REGION=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['region'])")
PREFIX=$(python3 -c "import json; print(json.load(open('cdk.json'))['context'].get('stack_prefix', 'OpenClaw'))")

TABLE_NAME=$(aws cloudformation describe-stacks \
    --stack-name "${PREFIX}Router" \
    --query "Stacks[0].Outputs[?OutputKey=='IdentityTableName'].OutputValue" \
    --output text --region "$REGION")

ACTION="${1:-list}"
CHANNEL_ID="${2:-}"
DISPLAY_NAME="${3:-User}"

case "$ACTION" in
    add)
        if [ -z "$CHANNEL_ID" ]; then
            echo "Usage: $0 add <channel:user_id> [display_name]"
            echo "  e.g. $0 add telegram:123456789 Alice"
            exit 1
        fi
        CHANNEL=$(echo "$CHANNEL_ID" | cut -d: -f1)
        UID=$(echo "$CHANNEL_ID" | cut -d: -f2)
        USER_ID="user_${CHANNEL}_${UID}"

        aws dynamodb put-item \
            --table-name "$TABLE_NAME" \
            --item "{
                \"pk\": {\"S\": \"USER#${USER_ID}\"},
                \"sk\": {\"S\": \"PROFILE\"},
                \"user_id\": {\"S\": \"${USER_ID}\"},
                \"channel_id\": {\"S\": \"${CHANNEL_ID}\"},
                \"display_name\": {\"S\": \"${DISPLAY_NAME}\"},
                \"created_at\": {\"N\": \"$(date +%s)\"},
                \"status\": {\"S\": \"active\"}
            }" \
            --region "$REGION"
        echo "✓ Added $CHANNEL_ID ($DISPLAY_NAME)"
        ;;

    remove)
        if [ -z "$CHANNEL_ID" ]; then
            echo "Usage: $0 remove <channel:user_id>"
            exit 1
        fi
        CHANNEL=$(echo "$CHANNEL_ID" | cut -d: -f1)
        UID=$(echo "$CHANNEL_ID" | cut -d: -f2)
        USER_ID="user_${CHANNEL}_${UID}"

        aws dynamodb delete-item \
            --table-name "$TABLE_NAME" \
            --key "{
                \"pk\": {\"S\": \"USER#${USER_ID}\"},
                \"sk\": {\"S\": \"PROFILE\"}
            }" \
            --region "$REGION"
        echo "✓ Removed $CHANNEL_ID"
        ;;

    list)
        echo "Registered users:"
        echo "━━━━━━━━━━━━━━━━"
        aws dynamodb scan \
            --table-name "$TABLE_NAME" \
            --filter-expression "sk = :sk" \
            --expression-attribute-values '{":sk": {"S": "PROFILE"}}' \
            --projection-expression "channel_id, display_name, #s" \
            --expression-attribute-names '{"#s": "status"}' \
            --region "$REGION" \
            --output table
        ;;

    *)
        echo "Usage: $0 {add|remove|list} [channel:user_id] [display_name]"
        exit 1
        ;;
esac
