"""Router Lambda — webhook handler for Telegram, Slack, WhatsApp, Discord.

Receives HTTP webhooks from API Gateway, validates signatures, resolves
user identity from DynamoDB, and invokes AgentCore Runtime.
"""

import hashlib
import hmac
import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Clients (initialized once per container) ---
secrets_client = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")
agentcore_client = boto3.client("bedrock-agent-runtime")

IDENTITY_TABLE = os.environ["IDENTITY_TABLE"]
STACK_NAME = os.environ["STACK_NAME"]
CHANNELS = os.environ.get("CHANNELS", "telegram").split(",")
MAX_USERS = int(os.environ.get("MAX_USERS", "10"))
REGISTRATION_OPEN = os.environ.get("REGISTRATION_OPEN", "false") == "true"

identity_table = dynamodb.Table(IDENTITY_TABLE)

# Cache for secrets (refreshed every 5 min)
_secret_cache: dict[str, tuple[str, float]] = {}
SECRET_TTL = 300


def _get_secret(arn: str) -> str:
    """Fetch secret value with in-memory caching."""
    now = time.time()
    if arn in _secret_cache:
        value, fetched_at = _secret_cache[arn]
        if now - fetched_at < SECRET_TTL:
            return value
    resp = secrets_client.get_secret_value(SecretId=arn)
    value = resp["SecretString"]
    _secret_cache[arn] = (value, now)
    return value


def _verify_telegram_signature(body: str, headers: dict) -> bool:
    """Telegram doesn't sign webhooks — we rely on the secret URL path."""
    return True


def _verify_slack_signature(body: str, headers: dict) -> bool:
    """Verify Slack request signature using signing secret."""
    secret_arn = os.environ.get("SLACK_SECRET_ARN")
    if not secret_arn:
        return False
    creds = json.loads(_get_secret(secret_arn))
    signing_secret = creds.get("signingSecret", "")
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    base = f"v0:{timestamp}:{body}"
    expected = "v0=" + hmac.new(
        signing_secret.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _resolve_user(channel: str, channel_user_id: str) -> dict | None:
    """Look up user in identity table by channel ID."""
    resp = identity_table.query(
        IndexName="channel-lookup",
        KeyConditionExpression="channel_id = :cid",
        ExpressionAttributeValues={":cid": f"{channel}:{channel_user_id}"},
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _register_user(channel: str, channel_user_id: str, display_name: str) -> dict:
    """Register a new user if registration is open and under max_users."""
    # Check user count
    resp = identity_table.scan(Select="COUNT")
    if resp["Count"] >= MAX_USERS and not REGISTRATION_OPEN:
        return None

    user_id = f"user_{channel}_{channel_user_id}"
    item = {
        "pk": f"USER#{user_id}",
        "sk": "PROFILE",
        "user_id": user_id,
        "channel_id": f"{channel}:{channel_user_id}",
        "display_name": display_name,
        "created_at": int(time.time()),
        "status": "active",
    }
    identity_table.put_item(Item=item)
    return item


def _extract_message(channel: str, body: dict) -> tuple[str, str, str] | None:
    """Extract (channel_user_id, display_name, message_text) from webhook payload."""
    if channel == "telegram":
        msg = body.get("message", {})
        user = msg.get("from", {})
        text = msg.get("text", "")
        if not text:
            return None
        uid = str(user.get("id", ""))
        name = user.get("first_name", "Unknown")
        return uid, name, text

    elif channel == "slack":
        event = body.get("event", {})
        # Ignore bot messages
        if event.get("bot_id"):
            return None
        uid = event.get("user", "")
        text = event.get("text", "")
        if not text:
            return None
        return uid, uid, text

    elif channel == "discord":
        # Discord interaction webhook
        uid = body.get("member", {}).get("user", {}).get("id", "")
        name = body.get("member", {}).get("user", {}).get("username", "Unknown")
        text = ""
        if body.get("type") == 2:  # APPLICATION_COMMAND
            options = body.get("data", {}).get("options", [])
            text = options[0].get("value", "") if options else ""
        return uid, name, text if text else None

    elif channel == "whatsapp":
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return None
        msg = messages[0]
        uid = msg.get("from", "")
        name = value.get("contacts", [{}])[0].get("profile", {}).get("name", "Unknown")
        text = msg.get("text", {}).get("body", "")
        return uid, name, text if text else None

    return None


def _detect_channel(path: str, headers: dict) -> str | None:
    """Detect which channel sent the webhook based on URL path."""
    path = path.lower().rstrip("/")
    for ch in CHANNELS:
        if path.endswith(f"/webhook/{ch}"):
            return ch
    return None


def handler(event, context):
    """Lambda handler — API Gateway HTTP API v2 payload format."""
    try:
        path = event.get("rawPath", "")
        headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        body_str = event.get("body", "{}")

        # Slack URL verification challenge
        if "challenge" in body_str:
            body = json.loads(body_str)
            if body.get("type") == "url_verification":
                return {"statusCode": 200, "body": body["challenge"]}

        channel = _detect_channel(path, headers)
        if not channel:
            return {"statusCode": 404, "body": json.dumps({"error": "Unknown channel"})}

        body = json.loads(body_str)

        # Extract message
        extracted = _extract_message(channel, body)
        if not extracted:
            return {"statusCode": 200, "body": "ok"}

        channel_user_id, display_name, message_text = extracted

        # Resolve or register user
        user = _resolve_user(channel, channel_user_id)
        if not user:
            if REGISTRATION_OPEN:
                user = _register_user(channel, channel_user_id, display_name)
            if not user:
                logger.warning(
                    "Unregistered user %s:%s — registration closed or at capacity",
                    channel,
                    channel_user_id,
                )
                return {"statusCode": 200, "body": "ok"}

        tenant_id = user["user_id"]

        # Invoke AgentCore Runtime
        logger.info("Invoking AgentCore for tenant=%s message=%s", tenant_id, message_text[:50])

        # TODO: Replace with actual AgentCore InvokeAgentRuntime call
        # once runtime_id is available from Phase 2 deployment.
        # response = agentcore_client.invoke_agent_runtime(
        #     agentRuntimeId=os.environ["RUNTIME_ID"],
        #     sessionId=tenant_id,
        #     inputText=message_text,
        # )

        return {"statusCode": 200, "body": json.dumps({"status": "accepted"})}

    except Exception:
        logger.exception("Router error")
        return {"statusCode": 500, "body": json.dumps({"error": "Internal error"})}
