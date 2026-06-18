"""Router Lambda — webhook handler for Telegram, Slack, WhatsApp, Discord.

Flow:
  1. API Gateway receives webhook from a messaging platform
  2. Validate signature (Slack, WhatsApp, Discord use HMAC; Telegram uses secret path)
  3. Extract (channel_user_id, display_name, message_text) from payload
  4. Resolve tenant_id from DynamoDB identity table
  5. Invoke AgentCore Runtime → server.py /invocations → openclaw agent CLI
  6. Send the response text back to the originating channel

tenant_id format expected by server.py:
  <channel_prefix>__<user_id>
  e.g.  tg__123456789   sl__U0123ABCD   wa__15551234567   dc__987654321

Channel prefixes (match server.py ch_map):
  tg = Telegram, sl = Slack, wa = WhatsApp, dc = Discord
"""

import hashlib
import hmac
import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS clients (initialized once per Lambda container) ──────────────────
secrets_client = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")

IDENTITY_TABLE = os.environ["IDENTITY_TABLE"]
RUNTIME_ID = os.environ.get("RUNTIME_ID", "")  # set after Phase 2
RUNTIME_ARN = os.environ.get("RUNTIME_ARN", "")  # full ARN for invoke_agent_runtime
STACK_NAME = os.environ.get("STACK_NAME", "OpenClaw")
CHANNELS = os.environ.get("CHANNELS", "telegram").split(",")
MAX_USERS = int(os.environ.get("MAX_USERS", "10"))
REGISTRATION_OPEN = os.environ.get("REGISTRATION_OPEN", "false") == "true"
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

identity_table = dynamodb.Table(IDENTITY_TABLE)

# AgentCore client — bedrock-agentcore service
agentcore_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)

# Channel prefix map (must match server.py ch_map)
CHANNEL_PREFIX = {
    "telegram": "tg",
    "slack": "sl",
    "whatsapp": "wa",
    "discord": "dc",
}

# ── Secret cache ─────────────────────────────────────────────────────────
_secret_cache: dict[str, tuple[str, float]] = {}
SECRET_TTL = 300  # seconds


def _get_secret(arn: str) -> str:
    now = time.time()
    if arn in _secret_cache:
        value, fetched_at = _secret_cache[arn]
        if now - fetched_at < SECRET_TTL:
            return value
    resp = secrets_client.get_secret_value(SecretId=arn)
    value = resp["SecretString"]
    _secret_cache[arn] = (value, now)
    return value


# ── Signature verification ────────────────────────────────────────────────


def _verify_slack_signature(body: str, headers: dict) -> bool:
    """Verify Slack request using signing secret (HMAC-SHA256)."""
    secret_arn = os.environ.get("SLACK_SECRET_ARN", "")
    if not secret_arn:
        return False
    try:
        creds = json.loads(_get_secret(secret_arn))
        signing_secret = creds.get("signingSecret", "")
        timestamp = headers.get("x-slack-request-timestamp", "")
        signature = headers.get("x-slack-signature", "")
        # Reject replays older than 5 minutes
        if abs(time.time() - int(timestamp)) > 300:
            return False
        base = f"v0:{timestamp}:{body}"
        expected = "v0=" + hmac.new(signing_secret.encode(), base.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        logger.warning("Slack signature verification failed: %s", e)
        return False


def _verify_whatsapp_signature(body: str, headers: dict) -> bool:
    """Verify WhatsApp Cloud API webhook using app secret (HMAC-SHA256)."""
    secret_arn = os.environ.get("WHATSAPP_SECRET_ARN", "")
    if not secret_arn:
        return False
    try:
        creds = json.loads(_get_secret(secret_arn))
        app_secret = creds.get("appSecret", creds.get("token", ""))
        signature = headers.get("x-hub-signature-256", "").removeprefix("sha256=")
        expected = hmac.new(app_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        logger.warning("WhatsApp signature verification failed: %s", e)
        return False


def _verify_discord_signature(body: str, headers: dict) -> bool:
    """Verify Discord interaction using Ed25519 public key."""
    secret_arn = os.environ.get("DISCORD_SECRET_ARN", "")
    if not secret_arn:
        return False
    try:
        from nacl.exceptions import BadSignatureError  # type: ignore
        from nacl.signing import VerifyKey  # type: ignore

        creds = json.loads(_get_secret(secret_arn))
        public_key = creds.get("publicKey", "")
        signature = headers.get("x-signature-ed25519", "")
        timestamp = headers.get("x-signature-timestamp", "")
        vk = VerifyKey(bytes.fromhex(public_key))
        vk.verify((timestamp + body).encode(), bytes.fromhex(signature))
        return True
    except Exception as e:
        logger.warning("Discord signature verification failed: %s", e)
        return False


# ── Message extraction ────────────────────────────────────────────────────


def _extract_message(channel: str, body: dict) -> tuple[str, str, str] | None:
    """Return (channel_user_id, display_name, message_text) or None to skip."""
    if channel == "telegram":
        msg = body.get("message") or body.get("edited_message", {})
        if not msg:
            return None
        user = msg.get("from", {})
        text = msg.get("text", "")
        if not text:
            return None
        uid = str(user.get("id", ""))
        name = user.get("first_name", "Unknown")
        return uid, name, text

    elif channel == "slack":
        # Ignore retries (Slack resends if we don't respond in 3s)
        event = body.get("event", {})
        if event.get("bot_id") or event.get("subtype"):
            return None
        uid = event.get("user", "")
        text = event.get("text", "")
        if not uid or not text:
            return None
        return uid, uid, text

    elif channel == "whatsapp":
        entry = (body.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return None
        msg = messages[0]
        if msg.get("type") != "text":
            return None  # skip images, audio, etc. for now
        uid = msg.get("from", "")
        name = (value.get("contacts") or [{}])[0].get("profile", {}).get("name", "Unknown")
        text = msg.get("text", {}).get("body", "")
        return (uid, name, text) if text else None

    elif channel == "discord":
        # Discord sends a PING (type=1) on webhook registration — must return pong
        if body.get("type") == 1:
            return "__discord_ping__", "", ""
        # Slash command (type=2) or message component (type=3)
        if body.get("type") not in (2, 3):
            return None
        member = body.get("member") or {}
        user = member.get("user") or body.get("user") or {}
        uid = user.get("id", "")
        name = user.get("username", "Unknown")
        options = body.get("data", {}).get("options", [])
        text = options[0].get("value", "") if options else ""
        return (uid, name, text) if text else None

    return None


# ── User identity ─────────────────────────────────────────────────────────


def _resolve_user(channel: str, channel_user_id: str) -> dict | None:
    """Look up user in identity table by channel_id GSI."""
    try:
        resp = identity_table.query(
            IndexName="channel-lookup",
            KeyConditionExpression="channel_id = :cid",
            ExpressionAttributeValues={":cid": f"{channel}:{channel_user_id}"},
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except ClientError as e:
        logger.error("DynamoDB query failed: %s", e)
        return None


def _register_user(channel: str, channel_user_id: str, display_name: str) -> dict | None:
    """Register a new user if under capacity."""
    try:
        resp = identity_table.scan(Select="COUNT")
        if resp["Count"] >= MAX_USERS:
            logger.warning("User capacity reached (%d/%d)", resp["Count"], MAX_USERS)
            return None
    except ClientError:
        return None

    prefix = CHANNEL_PREFIX.get(channel, channel[:2])
    user_id = f"{prefix}__{channel_user_id}"
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
    logger.info("Registered new user: %s (%s)", user_id, display_name)
    return item


# ── AgentCore invocation ──────────────────────────────────────────────────


def _send_ack(channel: str, channel_user_id: str, body: dict) -> None:
    """Best-effort 'working on it' nudge for long-running turns.

    Sent only by the delay timer in _invoke_and_reply, so it appears just for
    tasks that take longer than the threshold (e.g. large inbox scans). Failures
    are swallowed — the ack must never affect the real reply.
    """
    msg = "⏳ Working on it…"
    try:
        if channel == "telegram":
            _reply_telegram(channel_user_id, msg)
        elif channel == "slack":
            _reply_slack(channel_user_id, msg)
        elif channel == "whatsapp":
            _reply_whatsapp(channel_user_id, msg)
        # Discord uses single-use interaction tokens — skip to avoid consuming it.
    except Exception as e:
        logger.warning("ack send failed (%s): %s", channel, e)


def _invoke_and_reply(tenant_id: str, message: str, channel: str, channel_user_id: str, body: dict) -> None:
    """Run in a background thread — invoke AgentCore then send reply.

    The Lambda returns 200 to the webhook platform immediately (within ~100ms).
    This thread continues running and completes before Lambda freezes.
    Lambda stays alive until all non-daemon threads finish.
    """
    import threading

    # Acknowledge only if the turn runs long, so the user isn't left in silence
    # on multi-minute tasks (big inbox scans, multi-step Workspace actions). The
    # timer is cancelled if the real response returns first — fast replies stay clean.
    ack_timer = threading.Timer(7.0, _send_ack, args=(channel, channel_user_id, body))
    ack_timer.daemon = True
    ack_timer.start()
    try:
        response_text = _invoke_agentcore(tenant_id, message)
    finally:
        ack_timer.cancel()

    if channel == "telegram":
        _reply_telegram(channel_user_id, response_text)
    elif channel == "slack":
        _reply_slack(channel_user_id, response_text)
    elif channel == "whatsapp":
        _reply_whatsapp(channel_user_id, response_text)
    elif channel == "discord":
        secret_arn = os.environ.get("DISCORD_SECRET_ARN", "")
        app_id = ""
        if secret_arn:
            try:
                creds = json.loads(_get_secret(secret_arn))
                app_id = creds.get("applicationId", "")
            except Exception:
                pass
        interaction_token = body.get("token", "")
        if app_id and interaction_token:
            _reply_discord(interaction_token, app_id, response_text)


def _invoke_agentcore(tenant_id: str, message: str) -> str:
    """Call AgentCore Runtime and return the response text.

    server.py /invocations expects:
      Header: X-Amzn-Bedrock-AgentCore-Runtime-Session-Id = tenant_id
      Body:   { "prompt": "<message>" }

    Returns the "response" field from the JSON body.
    """
    if not RUNTIME_ARN and not RUNTIME_ID:
        logger.error("RUNTIME_ARN not set — Phase 2 not yet deployed")
        return "I'm not fully deployed yet. Please check back soon."

    # Use ARN directly (preferred) or build it from the ID
    runtime_arn = RUNTIME_ARN or (
        f"arn:aws:bedrock-agentcore:{AWS_REGION}:{os.environ.get('AWS_ACCOUNT_ID', '')}:runtime/{RUNTIME_ID}"
    )

    # runtimeSessionId must be ≥33 chars — pad with zeros if needed
    session_id = tenant_id.ljust(33, "0")

    try:
        resp = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps({"prompt": message}).encode(),
        )
        # Response is a streaming body
        body_bytes = resp["response"].read()
        logger.info("AgentCore raw response (%d bytes): %s", len(body_bytes), body_bytes[:500])
        data = json.loads(body_bytes)
        if data.get("error"):
            logger.error("AgentCore container error: %s", data["error"])
            return f"Agent error: {data['error']}"
        return data.get("response", "(no response)")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        logger.error("AgentCore invocation failed: %s %s", code, e)
        if code == "ThrottlingException":
            return "I'm a bit busy right now — please try again in a moment."
        return "Something went wrong on my end. Please try again."
    except Exception as e:
        logger.error("AgentCore invocation error: %s", e)
        return "Something went wrong on my end. Please try again."


# ── Channel reply senders ─────────────────────────────────────────────────


TELEGRAM_MAX_LEN = 4096


def _md_to_telegram_html(text: str) -> str:
    """Convert the agent's Markdown into Telegram-compatible HTML.

    Telegram's HTML parse mode supports a small tag set (<b> <i> <u> <s>
    <code> <pre> <a>) and only requires escaping & < >. That is far more
    robust than MarkdownV2, which 400s on any unescaped special char — which
    is why raw Markdown was previously sent as plain text and showed literal
    ** / ### / | characters. Constructs Telegram can't render (headings,
    tables) are downgraded: headings -> bold, tables/code fences -> monospace
    <pre> so columns still line up.
    """
    import re

    placeholders: list[str] = []

    def _stash(html: str) -> str:
        placeholders.append(html)
        return f"\x00{len(placeholders) - 1}\x00"

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 1. Fenced code blocks ```lang\n...``` -> <pre> (kept atomic via placeholder)
    text = re.sub(
        r"```[ \t]*[\w+-]*\n?(.*?)```",
        lambda m: _stash(f"<pre>{_esc(m.group(1))}</pre>"),
        text,
        flags=re.DOTALL,
    )

    # 2. Markdown tables -> monospace <pre> (Telegram has no table support).
    #    A table = a row containing '|' immediately followed by a |---|---|
    #    separator row.
    sep_re = re.compile(r"^\s*\|?\s*:?-{2,}.*$")
    src_lines = text.split("\n")
    out_lines: list[str] = []
    i = 0
    while i < len(src_lines):
        line = src_lines[i]
        nxt = src_lines[i + 1] if i + 1 < len(src_lines) else ""
        if "|" in line and "|" in nxt and sep_re.match(nxt):
            tbl = [line, nxt]
            i += 2
            while i < len(src_lines) and "|" in src_lines[i]:
                tbl.append(src_lines[i])
                i += 1
            out_lines.append(_stash("<pre>" + _esc("\n".join(tbl)) + "</pre>"))
        else:
            out_lines.append(line)
            i += 1
    text = "\n".join(out_lines)

    # 3. Inline code `code` -> <code>
    text = re.sub(r"`([^`\n]+)`", lambda m: _stash(f"<code>{_esc(m.group(1))}</code>"), text)

    # 4. Escape remaining text (placeholders are inert — no & < >)
    text = _esc(text)

    # 5. Inline Markdown -> HTML
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)  # links
    text = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*$", r"<b>\1</b>", text)  # headings
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)  # **bold**
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)  # __bold__
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.DOTALL)  # ~~strike~~
    # *italic* — single star, not adjacent to another star or word char (avoids
    # mangling snake_case, math, etc.)
    text = re.sub(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", text)

    # 6. Restore placeholders
    text = re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)
    return text


def _strip_telegram_html(html: str) -> str:
    """Best-effort plain-text fallback: drop tags and unescape entities."""
    import re

    text = re.sub(r"<[^>]+>", "", html)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _split_for_telegram(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Split into <=limit chunks, preferring newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
                current = ""
            if len(line) > limit:
                for j in range(0, len(line), limit):
                    chunks.append(line[j : j + limit])
                continue
        current = line if not current else current + "\n" + line
    if current:
        chunks.append(current)
    return chunks


def _reply_telegram(channel_user_id: str, text: str) -> None:
    """Send reply via Telegram Bot API, rendering Markdown as Telegram HTML."""
    import urllib.request

    secret_arn = os.environ.get("TELEGRAM_SECRET_ARN", "")
    if not secret_arn:
        return
    token = _get_secret(secret_arn)
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _send(chunk: str, as_html: bool) -> bool:
        body = {
            "chat_id": channel_user_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if as_html:
            body["parse_mode"] = "HTML"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)  # nosec B310
            return True
        except Exception as e:
            logger.warning("Telegram send failed (html=%s): %s", as_html, e)
            return False

    html = _md_to_telegram_html(text)
    for chunk in _split_for_telegram(html):
        if not _send(chunk, as_html=True):
            # Never lose a message to a formatting error — resend as plain text.
            _send(_strip_telegram_html(chunk), as_html=False)


def _reply_slack(channel_user_id: str, text: str) -> None:
    """Send reply via Slack Web API (DM to user)."""
    import urllib.request

    secret_arn = os.environ.get("SLACK_SECRET_ARN", "")
    if not secret_arn:
        return
    creds = json.loads(_get_secret(secret_arn))
    bot_token = creds.get("botToken", "")
    # Open a DM channel first
    url_open = "https://slack.com/api/conversations.open"
    payload_open = json.dumps({"users": channel_user_id}).encode()
    req = urllib.request.Request(
        url_open,
        data=payload_open,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {bot_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310
            dm_data = json.loads(r.read())
        dm_channel = dm_data.get("channel", {}).get("id", "")
    except Exception as e:
        logger.error("Slack open DM failed: %s", e)
        return

    url_msg = "https://slack.com/api/chat.postMessage"
    payload_msg = json.dumps({"channel": dm_channel, "text": text}).encode()
    req2 = urllib.request.Request(
        url_msg, data=payload_msg, headers={"Content-Type": "application/json", "Authorization": f"Bearer {bot_token}"}
    )
    try:
        urllib.request.urlopen(req2, timeout=10)  # nosec B310
    except Exception as e:
        logger.error("Slack reply failed: %s", e)


def _reply_whatsapp(channel_user_id: str, text: str) -> None:
    """Send reply via WhatsApp Cloud API."""
    import urllib.request

    secret_arn = os.environ.get("WHATSAPP_SECRET_ARN", "")
    if not secret_arn:
        return
    creds = json.loads(_get_secret(secret_arn))
    token = creds.get("token", "")
    phone_number_id = creds.get("phoneNumberId", "")
    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    payload = json.dumps(
        {
            "messaging_product": "whatsapp",
            "to": channel_user_id,
            "type": "text",
            "text": {"body": text},
        }
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)  # nosec B310
    except Exception as e:
        logger.error("WhatsApp reply failed: %s", e)


def _reply_discord(interaction_token: str, application_id: str, text: str) -> None:
    """Send reply via Discord interaction followup (deferred response)."""
    import urllib.request

    url = f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}"
    payload = json.dumps({"content": text}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)  # nosec B310
    except Exception as e:
        logger.error("Discord reply failed: %s", e)


# ── Lambda handler ────────────────────────────────────────────────────────


def handler(event, context):
    """API Gateway HTTP API v2 payload format."""
    try:
        path = event.get("rawPath", "")
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        body_str = event.get("body") or "{}"
        is_base64 = event.get("isBase64Encoded", False)
        if is_base64:
            import base64

            body_str = base64.b64decode(body_str).decode()

        # ── WhatsApp webhook verification (GET with hub.challenge) ────────
        if event.get("requestContext", {}).get("http", {}).get("method") == "GET":
            qs = event.get("queryStringParameters") or {}
            if qs.get("hub.mode") == "subscribe":
                secret_arn = os.environ.get("WHATSAPP_SECRET_ARN", "")
                verify_token = ""
                if secret_arn:
                    try:
                        creds = json.loads(_get_secret(secret_arn))
                        verify_token = creds.get("verifyToken", "")
                    except Exception:
                        pass
                if qs.get("hub.verify_token") == verify_token:
                    return {"statusCode": 200, "body": qs.get("hub.challenge", "")}
                return {"statusCode": 403, "body": "Forbidden"}

        # ── Slack URL verification challenge ──────────────────────────────
        if '"type":"url_verification"' in body_str or "'type': 'url_verification'" in body_str:
            try:
                body_obj = json.loads(body_str)
                if body_obj.get("type") == "url_verification":
                    return {"statusCode": 200, "body": body_obj["challenge"]}
            except Exception:
                pass

        # ── Detect channel from path ──────────────────────────────────────
        channel = None
        path_lower = path.lower().rstrip("/")
        for ch in CHANNELS:
            if path_lower.endswith(f"/webhook/{ch}"):
                channel = ch
                break

        if not channel:
            logger.warning("Unknown webhook path: %s", path)
            return {"statusCode": 404, "body": json.dumps({"error": "Unknown channel"})}

        # ── Signature verification ────────────────────────────────────────
        if channel == "slack" and not _verify_slack_signature(body_str, headers):
            logger.warning("Slack signature verification failed")
            return {"statusCode": 401, "body": "Unauthorized"}

        if channel == "whatsapp" and not _verify_whatsapp_signature(body_str, headers):
            logger.warning("WhatsApp signature verification failed")
            return {"statusCode": 401, "body": "Unauthorized"}

        if channel == "discord" and not _verify_discord_signature(body_str, headers):
            logger.warning("Discord signature verification failed")
            return {"statusCode": 401, "body": "Unauthorized"}

        body = json.loads(body_str)

        # ── Discord PING (webhook registration handshake) ─────────────────
        if channel == "discord" and body.get("type") == 1:
            return {"statusCode": 200, "body": json.dumps({"type": 1})}

        # ── Extract message ───────────────────────────────────────────────
        extracted = _extract_message(channel, body)
        if not extracted:
            return {"statusCode": 200, "body": "ok"}

        channel_user_id, display_name, message_text = extracted

        # ── Resolve or register user ──────────────────────────────────────
        user = _resolve_user(channel, channel_user_id)
        if not user:
            if REGISTRATION_OPEN:
                user = _register_user(channel, channel_user_id, display_name)
            if not user:
                logger.warning("Unregistered user %s:%s", channel, channel_user_id)
                # Silently accept — don't reveal the allowlist to strangers
                return {"statusCode": 200, "body": "ok"}

        # tenant_id format: <prefix>__<channel_user_id>  (e.g. tg__123456789)
        # server.py uses this to locate the S3 workspace and DynamoDB records
        tenant_id = user["user_id"]

        logger.info("Routing %s → tenant=%s msg_len=%d", channel, tenant_id, len(message_text))

        # ── Invoke AgentCore and send reply synchronously ─────────────────
        # Lambda freezes the process immediately after handler returns, so
        # background threads are unreliable. We call AgentCore synchronously
        # within the 30s Lambda timeout. Telegram/Slack/WhatsApp all tolerate
        # webhook responses up to 30s. Discord needs a 3s ACK so we use the
        # deferred response pattern for that channel only.
        if channel == "discord":
            # Discord: return ACK immediately, reply via followup webhook
            import threading

            t = threading.Thread(
                target=_invoke_and_reply,
                args=(tenant_id, message_text, channel, channel_user_id, body),
                daemon=False,
            )
            t.start()
            t.join(timeout=25)  # wait up to 25s before Lambda returns
            return {
                "statusCode": 200,
                "body": json.dumps({"type": 5}),
                "headers": {"Content-Type": "application/json"},
            }

        # For all other channels: invoke synchronously then reply
        response_text = _invoke_agentcore(tenant_id, message_text)
        if channel == "telegram":
            _reply_telegram(channel_user_id, response_text)
        elif channel == "slack":
            _reply_slack(channel_user_id, response_text)
        elif channel == "whatsapp":
            _reply_whatsapp(channel_user_id, response_text)

        return {"statusCode": 200, "body": json.dumps({"status": "ok"})}

    except Exception:
        logger.exception("Router unhandled error")
        return {"statusCode": 500, "body": json.dumps({"error": "Internal error"})}
