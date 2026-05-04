#!/usr/bin/env python3
"""
Minimal AgentCore startup script.

1. Writes openclaw.json with correct region/model from env vars
2. Injects AWS credentials into environment for Node.js subprocess
3. Starts server.py (which handles /ping and /invocations)

This bypasses the complex entrypoint.sh entirely.
"""
import json
import logging
import os
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agentcore_start")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
WORKSPACE = "/root/.openclaw/workspace"

# ── Step 1: Write openclaw.json ───────────────────────────────────────────
config_dir = os.path.expanduser("~/.openclaw")
os.makedirs(config_dir, exist_ok=True)
os.makedirs(WORKSPACE, exist_ok=True)
os.makedirs(os.path.join(WORKSPACE, "memory"), exist_ok=True)

openclaw_config = {
    "models": {
        "providers": {
            "amazon-bedrock": {
                "baseUrl": f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com",
                "auth": "aws-sdk",
                "api": "bedrock-converse-stream",
                "models": [
                    {
                        "id": BEDROCK_MODEL_ID,
                        "name": "Bedrock Model",
                        "reasoning": False,
                        "input": ["text"],
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        "contextWindow": 200000,
                        "maxTokens": 8192,
                    }
                ],
            }
        }
    },
    "agents": {
        "defaults": {
            "model": {"primary": f"amazon-bedrock/{BEDROCK_MODEL_ID}"},
            "workspace": WORKSPACE,
            "skipBootstrap": True,
            "compaction": {"mode": "default", "recentTurnsPreserve": 5},
        }
    },
    "tools": {"profile": "full", "deny": ["cron", "gateway"]},
    "gateway": {
        "port": 18789,
        "mode": "local",
        "bind": "lan",
        "auth": {"mode": "none"},
        "controlUi": {"allowedOrigins": ["*"], "allowInsecureAuth": True},
    },
    "plugins": {"entries": {}},
}

config_path = os.path.join(config_dir, "openclaw.json")
with open(config_path, "w") as f:
    json.dump(openclaw_config, f, indent=2)
log.info("openclaw.json written: region=%s model=%s", AWS_REGION, BEDROCK_MODEL_ID)

# Write a minimal SOUL.md if none exists
soul_path = os.path.join(WORKSPACE, "SOUL.md")
if not os.path.exists(soul_path):
    with open(soul_path, "w") as f:
        f.write("# OpenClaw Assistant\n\nYou are a helpful AI assistant powered by OpenClaw on Amazon Bedrock.\n")
    log.info("SOUL.md created")

# ── Step 2: Inject AWS credentials for Node.js subprocess ────────────────
try:
    import boto3
    session_creds = boto3.Session().get_credentials()
    if session_creds:
        frozen = session_creds.get_frozen_credentials()
        os.environ["AWS_ACCESS_KEY_ID"] = frozen.access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
        if frozen.token:
            os.environ["AWS_SESSION_TOKEN"] = frozen.token
        os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
        log.info("AWS credentials resolved and injected")
except Exception as e:
    log.warning("Could not pre-inject credentials (will retry per-request): %s", e)

# ── Step 3: Start OpenClaw Gateway in background (optional, for tools) ───
try:
    gw = subprocess.Popen(
        ["/root/.local/share/pnpm/openclaw", "gateway", "--port", "18789"],
        stdout=open("/tmp/gateway.log", "w"),
        stderr=subprocess.STDOUT,
    )
    log.info("Gateway started PID=%d", gw.pid)
except Exception as e:
    log.warning("Gateway start failed (non-fatal, tools may be limited): %s", e)

# ── Step 4: Run server.py (blocks until exit) ─────────────────────────────
log.info("Starting server.py on port 8080...")
os.environ["OPENCLAW_WORKSPACE"] = WORKSPACE
os.environ["OPENCLAW_SKIP_ONBOARDING"] = "1"
os.environ["OPENCLAW_SKIP_CRON"] = "1"

os.execv(sys.executable, [sys.executable, "/app/server.py"])
