"""
Agent Container HTTP server for Amazon Bedrock AgentCore.

Wraps `openclaw agent --session-id <tenant_id> --message <text> --json`
as a subprocess for each /invocations request.

Plan A: inject allowed tools into system prompt via SOUL.md prepend.
Plan E: audit response for blocked tool usage.
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from observability import log_agent_invocation, log_permission_denied
from permissions import read_permission_profile
from safety import validate_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Path to openclaw binary (system install in container, nvm on EC2)
_OPENCLAW_CANDIDATES = [
    "/usr/local/bin/openclaw",
    "/usr/bin/openclaw",
]

_TOOL_PATTERN = re.compile(
    r"\b(shell|browser|file_write|code_execution|install_skill|load_extension|eval)\b",
    re.IGNORECASE,
)


def _find_openclaw() -> str:
    for p in _OPENCLAW_CANDIDATES:
        if os.path.isfile(p):
            return p
    # Check pnpm global bin
    pnpm_home = os.environ.get("PNPM_HOME", os.path.expanduser("~/.local/share/pnpm"))
    pnpm_openclaw = os.path.join(pnpm_home, "openclaw")
    if os.path.isfile(pnpm_openclaw):
        return pnpm_openclaw
    # fallback: hope it's on PATH
    return "openclaw"


OPENCLAW_BIN = _find_openclaw()
logger.info("openclaw binary: %s", OPENCLAW_BIN)

# Track which tenants have had their workspace assembled
_assembled_tenants: set = set()
_assembly_lock = threading.Lock()

# Config version tracking
_config_version: str = ""
_config_version_checked_at: float = 0.0
_CONFIG_VERSION_CHECK_INTERVAL = 300

# Guardrail config
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

WORKSPACE = os.environ.get("OPENCLAW_WORKSPACE", "/root/.openclaw/workspace")
S3_BUCKET = os.environ.get("S3_BUCKET", "openclaw-tenants-000000000000")
STACK_NAME = os.environ.get("STACK_NAME", "dev")
AWS_REGION_RUNTIME = os.environ.get("AWS_REGION", "us-east-1")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", os.environ.get("STACK_NAME", "openclaw"))
DYNAMODB_REGION = os.environ.get("DYNAMODB_REGION", os.environ.get("AWS_REGION", "us-east-1"))
LOCAL_DEV = os.environ.get("LOCAL_DEV", "").lower() in ("1", "true", "yes")

if LOCAL_DEV:
    logger.info("LOCAL_DEV mode enabled — skipping S3/DynamoDB/SSM calls")


def _check_and_refresh_config_version() -> None:
    """Check DynamoDB CONFIG#global-version and clear assembly cache if changed."""
    global _config_version, _config_version_checked_at
    now = time.time()
    if now - _config_version_checked_at < _CONFIG_VERSION_CHECK_INTERVAL:
        return
    _config_version_checked_at = now
    try:
        import boto3 as _b3cv

        ddb = _b3cv.resource("dynamodb", region_name=DYNAMODB_REGION)
        resp = ddb.Table(DYNAMODB_TABLE).get_item(Key={"PK": "ORG#acme", "SK": "CONFIG#global-version"})
        new_version = resp.get("Item", {}).get("version", "")
        if new_version and new_version != _config_version:
            logger.info(
                "Global config version changed: %s → %s — clearing assembly cache for %d tenants",
                _config_version or "(initial)",
                new_version,
                len(_assembled_tenants),
            )
            _assembled_tenants.clear()
            _config_version = new_version
    except Exception as e:
        logger.warning("Config version check failed (non-fatal): %s", e)


def _append_conversation_turn(tenant_id: str, user_message: str, assistant_reply: str, model: str, duration_ms: int):
    """Append a user+assistant turn to DynamoDB CONV# AND local daily memory file."""
    from datetime import datetime, timezone

    ts_dt = datetime.now(timezone.utc)
    ts = ts_dt.isoformat()

    try:
        import boto3 as _b3_conv

        ddb = _b3_conv.resource("dynamodb", region_name=DYNAMODB_REGION)
        table = ddb.Table(DYNAMODB_TABLE)
        org_pk = "ORG#acme"
        session_sk = f"SESSION#{tenant_id[:40]}"

        try:
            resp = table.get_item(Key={"PK": org_pk, "SK": session_sk})
            turns = int(resp.get("Item", {}).get("turns", 0))
        except Exception:
            turns = 0

        seq_base = (turns - 1) * 2
        table.put_item(
            Item={
                "PK": org_pk,
                "SK": f"CONV#{tenant_id[:40]}#{seq_base:04d}",
                "sessionId": tenant_id[:40],
                "seq": seq_base,
                "role": "user",
                "content": user_message[:2000],
                "ts": ts,
            }
        )
        table.put_item(
            Item={
                "PK": org_pk,
                "SK": f"CONV#{tenant_id[:40]}#{seq_base + 1:04d}",
                "sessionId": tenant_id[:40],
                "seq": seq_base + 1,
                "role": "assistant",
                "content": assistant_reply[:4000],
                "ts": ts,
                "model": model,
                "durationMs": duration_ms,
            }
        )
    except Exception as e:
        logger.warning("CONV# write failed (non-fatal): %s", e)

    try:
        workspace = os.environ.get("OPENCLAW_WORKSPACE", "/root/.openclaw/workspace")
        memory_dir = os.path.join(workspace, "memory")
        os.makedirs(memory_dir, exist_ok=True)
        date_str = ts_dt.strftime("%Y-%m-%d")
        time_str = ts_dt.strftime("%H:%M UTC")
        daily_file = os.path.join(memory_dir, f"{date_str}.md")

        entry = f"\n## {time_str}\n**User:** {user_message[:300]}\n**Agent:** {assistant_reply[:300]}\n"
        with open(daily_file, "a", encoding="utf-8") as f:
            f.write(entry)
        logger.info("Memory checkpoint written: %s", daily_file)
    except Exception as e:
        logger.warning("Daily memory write failed (non-fatal): %s", e)


def _write_usage_to_dynamodb(
    tenant_id: str, base_id: str, usage: dict, model: str, duration_ms: int, message: str = ""
):
    """Fire-and-forget: write usage metrics, session, and audit entry to DynamoDB."""
    if LOCAL_DEV:
        return
    try:
        from datetime import datetime, timezone
        from decimal import Decimal

        import boto3 as _b3

        ddb = _b3.resource("dynamodb", region_name=DYNAMODB_REGION)
        table = ddb.Table(DYNAMODB_TABLE)
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        org_pk = "ORG#acme"

        input_tokens = int(usage.get("input", 0))
        output_tokens = int(usage.get("output", 0))
        total_tokens = int(usage.get("total", input_tokens + output_tokens))

        MODEL_PRICING = {
            "global.amazon.nova-2-lite-v1:0": {"input": 0.30, "output": 2.50},
            "us.amazon.nova-pro-v1:0": {"input": 0.80, "output": 3.20},
            "global.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.00, "output": 15.00},
            "global.anthropic.claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
            "global.anthropic.claude-opus-4-6-v1": {"input": 15.00, "output": 75.00},
            "global.anthropic.claude-opus-4-5-20251101-v1:0": {"input": 15.00, "output": 75.00},
            "global.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 0.80, "output": 4.00},
            "us.deepseek.r1-v1:0": {"input": 1.35, "output": 5.40},
            "us.meta.llama3-3-70b-instruct-v1:0": {"input": 0.72, "output": 0.72},
            "moonshotai.kimi-k2.5": {"input": 0.60, "output": 3.00},
        }
        pricing = MODEL_PRICING.get(model, {"input": 0.30, "output": 2.50})
        cost = Decimal(str(round((input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000, 6)))

        agent_type = "always-on" if os.environ.get("EFS_ENABLED") == "true" else "serverless"
        usage_key = f"USAGE#{base_id}/{agent_type}#{today}"
        table.update_item(
            Key={"PK": org_pk, "SK": usage_key},
            UpdateExpression="SET #d = :date, agentId = :aid, model = :model, agentType = :at, GSI1PK = :gsi1pk, GSI1SK = :gsi1sk ADD inputTokens :inp, outputTokens :out, requests :one, cost :cost",
            ExpressionAttributeNames={"#d": "date"},
            ExpressionAttributeValues={
                ":date": today,
                ":aid": base_id,
                ":model": model,
                ":at": agent_type,
                ":inp": input_tokens,
                ":out": output_tokens,
                ":one": 1,
                ":cost": cost,
                ":gsi1pk": "TYPE#usage",
                ":gsi1sk": f"USAGE#{today}#{base_id}/{agent_type}",
            },
        )

        session_id = tenant_id[:40]
        table.update_item(
            Key={"PK": org_pk, "SK": f"SESSION#{session_id}"},
            UpdateExpression="SET id = :id, agentId = :aid, employeeId = :eid, #s = :status, lastActive = :now, GSI1PK = :gsi1pk, GSI1SK = :gsi1sk ADD turns :one, tokensUsed :tokens",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":id": session_id,
                ":aid": base_id,
                ":eid": base_id,
                ":status": "active",
                ":now": now.isoformat(),
                ":one": 1,
                ":tokens": total_tokens,
                ":gsi1pk": "TYPE#session",
                ":gsi1sk": f"SESSION#{session_id}",
            },
        )
        logger.info("DynamoDB usage written: %s tokens=%d cost=%s", base_id, total_tokens, cost)

        # Audit entry
        actor_name = base_id
        try:
            emp_resp = table.get_item(Key={"PK": "ORG#acme", "SK": f"EMP#{base_id}"})
            emp_item = emp_resp.get("Item", {})
            if emp_item.get("name"):
                actor_name = emp_item["name"]
        except Exception:
            pass

        channel = "unknown"
        t_parts = tenant_id.split("__")
        if t_parts:
            ch_map = {
                "wa": "WhatsApp",
                "tg": "Telegram",
                "dc": "Discord",
                "sl": "Slack",
                "ms": "Teams",
                "im": "iMessage",
                "gc": "Google Chat",
                "web": "Web",
                "port": "Portal",
            }
            channel = ch_map.get(t_parts[0], t_parts[0].upper())

        detail_msg = message[:100] if message else "(no message)"
        audit_id = f"aud-{int(now.timestamp() * 1000)}"
        table.put_item(
            Item={
                "PK": "ORG#acme",
                "SK": f"AUDIT#{audit_id}",
                "GSI1PK": "TYPE#audit",
                "GSI1SK": f"AUDIT#{audit_id}",
                "id": audit_id,
                "timestamp": now.isoformat(),
                "eventType": "agent_invocation",
                "actorId": base_id,
                "actorName": actor_name,
                "targetType": "agent",
                "targetId": base_id,
                "channel": channel,
                "detail": f"{channel} chat: {detail_msg}",
                "status": "success",
                "durationMs": duration_ms,
                "model": model,
            }
        )
        logger.info("Audit entry written: %s channel=%s", audit_id, channel)
    except Exception as e:
        logger.warning("DynamoDB usage write failed (non-fatal): %s", e)


def _ensure_workspace_assembled(tenant_id: str) -> None:
    """Assemble workspace on first invocation for a tenant."""
    if LOCAL_DEV:
        # In local dev, mark as assembled immediately — no S3/DynamoDB needed.
        # OpenClaw uses its local SQLite + markdown workspace directly.
        _assembled_tenants.add(tenant_id)
        return
    if tenant_id in _assembled_tenants or tenant_id == "unknown":
        return

    with _assembly_lock:
        if tenant_id in _assembled_tenants:
            return

        logger.info("First invocation for tenant %s — assembling workspace", tenant_id)

        base_id = tenant_id
        parts = tenant_id.split("__")
        if len(parts) >= 3:
            base_id = parts[1]
        elif len(parts) == 2:
            base_id = parts[1]

        # Resolve IM channel user IDs to employee IDs
        if not base_id.startswith("emp-"):
            try:
                import boto3 as _b3_mapping

                ddb = _b3_mapping.resource(
                    "dynamodb", region_name=os.environ.get("DYNAMODB_REGION", os.environ.get("AWS_REGION", "us-east-1"))
                )
                table = ddb.Table(os.environ.get("DYNAMODB_TABLE", os.environ.get("STACK_NAME", "openclaw")))
                channel_prefix = parts[0] if len(parts) >= 2 else ""
                resp_ddb = table.get_item(Key={"PK": "ORG#acme", "SK": f"MAPPING#{channel_prefix}__{base_id}"})
                ddb_item = resp_ddb.get("Item")
                if ddb_item:
                    resolved = ddb_item.get("employeeId", "")
                    logger.info("DynamoDB user-mapping resolved: %s__%s → %s", channel_prefix, base_id, resolved)
                    base_id = resolved
                else:
                    from boto3.dynamodb.conditions import Attr as _Attr
                    from boto3.dynamodb.conditions import Key as _Key

                    scan_resp = table.query(
                        KeyConditionExpression=_Key("PK").eq("ORG#acme") & _Key("SK").begins_with("MAPPING#"),
                        FilterExpression=_Attr("channelUserId").eq(base_id),
                    )
                    if scan_resp.get("Items"):
                        resolved = scan_resp["Items"][0].get("employeeId", "")
                        logger.info("DynamoDB user-mapping (scan) resolved: %s → %s", base_id, resolved)
                        base_id = resolved
                    else:
                        ssm = _b3_mapping.client("ssm", region_name=AWS_REGION_RUNTIME)
                        for mapping_key in [f"{channel_prefix}__{base_id}", base_id]:
                            try:
                                resp = ssm.get_parameter(Name=f"/openclaw/{STACK_NAME}/user-mapping/{mapping_key}")
                                resolved = resp["Parameter"]["Value"]
                                logger.info("SSM user-mapping fallback resolved: %s → %s", mapping_key, resolved)
                                base_id = resolved
                                break
                            except Exception:
                                pass
            except Exception as e:
                logger.warning("User-mapping lookup failed: %s", e)

        # Load shared credentials
        creds_dir = "/root/.openclaw/credentials"
        os.makedirs(creds_dir, exist_ok=True)
        try:
            subprocess.run(
                [
                    "aws",
                    "s3",
                    "cp",
                    f"s3://{S3_BUCKET}/_shared/openclaw-creds/discord-default-allowFrom.json",
                    f"{creds_dir}/discord-default-allowFrom.json",
                    "--quiet",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as e:
            logger.warning("Could not load discord credentials (non-fatal): %s", e)

        # Sync tenant's personal workspace from S3
        s3_base = f"s3://{S3_BUCKET}/{base_id}"
        try:
            subprocess.run(
                ["aws", "s3", "cp", f"{s3_base}/workspace/", f"{WORKSPACE}/", "--recursive", "--quiet"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            logger.info("S3 workspace copied for tenant %s (base: %s)", tenant_id, base_id)
        except Exception as e:
            logger.warning("S3 cp failed for %s: %s", tenant_id, e)

        # Run workspace_assembler.py
        assembler = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace_assembler.py")
        if os.path.isfile(assembler):
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        assembler,
                        "--tenant",
                        tenant_id,
                        "--workspace",
                        WORKSPACE,
                        "--bucket",
                        S3_BUCKET,
                        "--stack",
                        STACK_NAME,
                        "--region",
                        AWS_REGION_RUNTIME,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    logger.info("Workspace assembled for %s: %s", tenant_id, result.stdout.strip().split("\n")[-1])
                else:
                    logger.warning("Workspace assembly failed for %s: %s", tenant_id, result.stderr[:200])
            except Exception as e:
                logger.warning("Workspace assembly error for %s: %s", tenant_id, e)

        # Re-source skill env vars
        skill_env = "/tmp/skill_env.sh"
        if os.path.isfile(skill_env):
            try:
                with open(skill_env) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("export ") and "=" in line:
                            kv = line[7:]
                            key, _, val = kv.partition("=")
                            os.environ[key] = val.strip("'\"")
            except IOError:
                pass

        # Write tenant_id files
        try:
            with open("/tmp/tenant_id", "w") as f:
                f.write(tenant_id)
            with open("/tmp/base_tenant_id", "w") as f:
                f.write(base_id)
            logger.info("Base tenant ID written: %s", base_id)
        except IOError:
            pass

        # Synthesize MEMORY.md from daily memory files
        try:
            memory_md_path = os.path.join(WORKSPACE, "MEMORY.md")
            memory_dir = os.path.join(WORKSPACE, "memory")
            current_content = ""
            if os.path.isfile(memory_md_path):
                with open(memory_md_path) as f:
                    current_content = f.read().strip()

            if len(current_content) < 50 and os.path.isdir(memory_dir):
                daily_files = sorted([f for f in os.listdir(memory_dir) if f.endswith(".md")], reverse=True)[:3]
                if daily_files:
                    parts_mem = ["# Memory\n\n*Auto-synthesized from recent conversations*\n"]
                    for fname in daily_files:
                        fpath = os.path.join(memory_dir, fname)
                        try:
                            with open(fpath) as f:
                                content = f.read().strip()
                            if content:
                                date_str = fname.replace(".md", "")
                                parts_mem.append(f"\n## {date_str}\n{content[:3000]}")
                        except Exception:
                            pass
                    if len(parts_mem) > 1:
                        with open(memory_md_path, "w") as f:
                            f.write("\n".join(parts_mem))
                        logger.info("MEMORY.md synthesized from %d daily files for %s", len(daily_files), base_id)
        except Exception as e:
            logger.warning("MEMORY.md synthesis failed (non-fatal): %s", e)

        # Dynamic agent config from DynamoDB
        try:
            import boto3 as _b3_model

            ddb = _b3_model.resource("dynamodb", region_name=DYNAMODB_REGION)
            table = ddb.Table(DYNAMODB_TABLE)

            pos_id = ""
            try:
                emp_resp = table.get_item(Key={"PK": "ORG#acme", "SK": f"EMP#{base_id}"})
                pos_id = emp_resp.get("Item", {}).get("positionId", "")
            except Exception:
                pass

            model_config_resp = table.get_item(Key={"PK": "ORG#acme", "SK": "CONFIG#model"})
            if "Item" in model_config_resp:
                mc = model_config_resp["Item"]
                emp_model_overrides = mc.get("employeeOverrides", {})
                pos_model_overrides = mc.get("positionOverrides", {})

                if base_id in emp_model_overrides:
                    new_model_id = emp_model_overrides[base_id].get("modelId", "")
                elif pos_id and pos_id in pos_model_overrides:
                    new_model_id = pos_model_overrides[pos_id].get("modelId", "")
                else:
                    new_model_id = mc.get("default", {}).get("modelId", "")

                if new_model_id:
                    oc_config_path = os.path.expanduser("~/.openclaw/openclaw.json")
                    if os.path.isfile(oc_config_path):
                        with open(oc_config_path) as f:
                            oc_config = json.load(f)
                        old_model = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6")
                        oc_json_str = json.dumps(oc_config)
                        oc_json_str = oc_json_str.replace(old_model, new_model_id)
                        with open(oc_config_path, "w") as f:
                            f.write(oc_json_str)
                        os.environ["BEDROCK_MODEL_ID"] = new_model_id
                        logger.info("Model updated to %s", new_model_id)

            agent_cfg_resp = table.get_item(Key={"PK": "ORG#acme", "SK": "CONFIG#agent-config"})
            if "Item" in agent_cfg_resp:
                agent_cfg = agent_cfg_resp["Item"]
                emp_cfg = agent_cfg.get("employeeConfig", {}).get(base_id, {})
                pos_cfg = agent_cfg.get("positionConfig", {}).get(pos_id, {}) if pos_id else {}
                eff_cfg = {**pos_cfg, **emp_cfg}

                oc_config_path = os.path.expanduser("~/.openclaw/openclaw.json")
                if eff_cfg and os.path.isfile(oc_config_path):
                    with open(oc_config_path) as f:
                        oc = json.load(f)
                    changed = False

                    if "recentTurnsPreserve" in eff_cfg:
                        oc.setdefault("agents", {}).setdefault("defaults", {}).setdefault("compaction", {})[
                            "recentTurnsPreserve"
                        ] = int(eff_cfg["recentTurnsPreserve"])
                        changed = True
                    if "compactionMode" in eff_cfg:
                        oc.setdefault("agents", {}).setdefault("defaults", {}).setdefault("compaction", {})["mode"] = (
                            eff_cfg["compactionMode"]
                        )
                        changed = True
                    if "maxTokens" in eff_cfg:
                        for provider in oc.get("models", {}).get("providers", {}).values():
                            for m in provider.get("models", []):
                                m["maxTokens"] = int(eff_cfg["maxTokens"])
                        changed = True

                    if changed:
                        with open(oc_config_path, "w") as f:
                            json.dump(oc, f, indent=2)
                        logger.info("Agent config applied for %s: %s", base_id, list(eff_cfg.keys()))
        except Exception as e:
            logger.warning("Dynamic agent config failed (non-fatal): %s", e)

        # Mirror workspace files to Gateway default path if different
        default_workspace = os.path.expanduser("~/.openclaw/workspace")
        if default_workspace != WORKSPACE:
            try:
                import shutil

                os.makedirs(default_workspace, exist_ok=True)
                for fname in ["SOUL.md", "AGENTS.md", "TOOLS.md", "IDENTITY.md", "CHANNELS.md"]:
                    src = os.path.join(WORKSPACE, fname)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(default_workspace, fname))
                logger.info("Mirrored workspace files to Gateway default path: %s", default_workspace)
            except Exception as e:
                logger.warning("Gateway workspace mirror failed (non-fatal): %s", e)

        # Write SOUL hash to DynamoDB
        try:
            import hashlib as _hl

            soul_path = os.path.join(WORKSPACE, "SOUL.md")
            soul_hash = ""
            if os.path.isfile(soul_path):
                with open(soul_path, "rb") as f:
                    soul_hash = _hl.sha256(f.read()).hexdigest()[:16]
            from datetime import datetime, timezone

            import boto3 as _b3sh

            ddb_sh = _b3sh.resource("dynamodb", region_name=DYNAMODB_REGION)
            session_key = tenant_id[:40]
            ddb_sh.Table(DYNAMODB_TABLE).update_item(
                Key={"PK": "ORG#acme", "SK": f"SESSION#{session_key}"},
                UpdateExpression="SET soulHash = :h, configVersion = :v, assembledAt = :t",
                ExpressionAttributeValues={
                    ":h": soul_hash,
                    ":v": _config_version or "initial",
                    ":t": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            logger.warning("SOUL hash write failed (non-fatal): %s", e)

        _assembled_tenants.add(tenant_id)
        logger.info("Workspace ready for tenant %s", tenant_id)


def _build_system_prompt(tenant_id: str) -> str:
    """Plan A: build constraint text to prepend to SOUL.md."""
    try:
        profile = read_permission_profile(tenant_id)
        allowed = profile.get("tools", ["web_search"])
        blocked = [
            t
            for t in [
                "shell",
                "browser",
                "file",
                "file_write",
                "code_execution",
                "install_skill",
                "load_extension",
                "eval",
            ]
            if t not in allowed
        ]
    except Exception:
        allowed = ["web_search"]
        blocked = [
            "shell",
            "browser",
            "file",
            "file_write",
            "code_execution",
            "install_skill",
            "load_extension",
            "eval",
        ]

    lines = [f"Allowed tools for this session: {', '.join(allowed)}."]
    if blocked:
        lines.append(
            f"You MUST NOT use these tools: {', '.join(blocked)}. "
            "If the user requests an action requiring a blocked tool, "
            "explain that you don't have permission."
        )
    return " ".join(lines)


def _audit_response(tenant_id: str, response_text: str, allowed_tools: list) -> None:
    """Plan E: scan response for blocked tool usage."""
    matches = _TOOL_PATTERN.findall(response_text)
    if not matches:
        return
    for tool in set(t.lower() for t in matches):
        if tool not in allowed_tools:
            log_permission_denied(
                tenant_id=tenant_id,
                tool_name=tool,
                cedar_decision="RESPONSE_AUDIT",
                request_id=None,
            )
            logger.warning("AUDIT: blocked tool '%s' in response tenant_id=%s", tool, tenant_id)


def _sync_heartbeat_and_memory(base_id: str) -> None:
    """Immediately sync HEARTBEAT.md and memory/*.md to S3 after each invocation."""
    if not base_id or base_id == "unknown":
        return
    sync_target = f"s3://{S3_BUCKET}/{base_id}/workspace/"
    try:
        subprocess.run(
            ["aws", "s3", "sync", os.path.join(WORKSPACE, "memory") + "/", f"{sync_target}memory/", "--quiet"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        heartbeat_path = os.path.join(WORKSPACE, "HEARTBEAT.md")
        if os.path.isfile(heartbeat_path):
            subprocess.run(
                ["aws", "s3", "cp", heartbeat_path, f"{sync_target}HEARTBEAT.md", "--quiet"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            logger.info("HEARTBEAT.md synced to S3 for %s", base_id)
        memory_md_path = os.path.join(WORKSPACE, "MEMORY.md")
        if os.path.isfile(memory_md_path):
            subprocess.run(
                ["aws", "s3", "cp", memory_md_path, f"{sync_target}MEMORY.md", "--quiet"],
                capture_output=True,
                text=True,
                timeout=10,
            )
    except Exception as e:
        logger.warning("Post-invocation S3 sync failed (non-fatal): %s", e)


def invoke_openclaw(tenant_id: str, message: str, timeout: int = 300, max_retries: int = 2) -> dict:
    """Run openclaw agent CLI with automatic retry on transient failures."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return _invoke_openclaw_once(tenant_id, message, timeout)
        except RuntimeError as e:
            last_error = e
            if attempt < max_retries:
                wait = (attempt + 1) * 2
                logger.warning("openclaw retry %d/%d after %ds: %s", attempt + 1, max_retries, wait, e)
                time.sleep(wait)
    raise last_error


def _invoke_openclaw_once(tenant_id: str, message: str, timeout: int = 300) -> dict:
    """Run: openclaw agent --session-id <tenant_id> --message <message> --json"""
    env = os.environ.copy()

    # Explicitly inject AWS credentials so the Node.js openclaw CLI can find them.
    # In AgentCore, credentials come via the container credentials endpoint
    # (AWS_CONTAINER_CREDENTIALS_RELATIVE_URI). boto3 resolves these fine, but
    # the Node.js AWS SDK may not pick them up in all environments.
    # We fetch them via boto3 and pass as explicit env vars.
    try:
        import boto3 as _b3creds

        session_creds = _b3creds.Session().get_credentials()
        if session_creds:
            frozen = session_creds.get_frozen_credentials()
            env["AWS_ACCESS_KEY_ID"] = frozen.access_key
            env["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
            if frozen.token:
                env["AWS_SESSION_TOKEN"] = frozen.token
            env["AWS_DEFAULT_REGION"] = env.get("AWS_REGION", "us-east-1")
            logger.info("AWS credentials injected for openclaw subprocess")
    except Exception as _cred_err:
        logger.warning("Could not inject AWS credentials: %s", _cred_err)

    skill_env_file = "/tmp/skill_env.sh"
    if os.path.isfile(skill_env_file):
        try:
            with open(skill_env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export ") and "=" in line:
                        kv = line[7:]
                        key, _, val = kv.partition("=")
                        val = val.strip("'\"")
                        env[key] = val
        except IOError:
            pass

    import glob as _glob

    nvm_bins = _glob.glob("/home/ubuntu/.nvm/versions/node/*/bin")
    if nvm_bins:
        env["PATH"] = nvm_bins[0] + ":" + env.get("PATH", "")
        env["HOME"] = "/home/ubuntu"

    openclaw_cmd = [
        OPENCLAW_BIN,
        "agent",
        "--session-id",
        tenant_id,
        "--message",
        message,
        "--json",
        "--timeout",
        str(timeout),
    ]

    run_env = None
    in_ecs = bool(os.environ.get("ECS_CONTAINER_METADATA_URI_V4"))
    if os.geteuid() == 0 and os.path.isdir("/home/ubuntu") and not in_ecs:
        path_val = env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        aws_region = env.get("AWS_REGION", "us-east-1")
        workspace_val = env.get("OPENCLAW_WORKSPACE", WORKSPACE)
        cmd = [
            "sudo",
            "-u",
            "ubuntu",
            "env",
            f"PATH={path_val}",
            "HOME=/home/ubuntu",
            f"AWS_REGION={aws_region}",
            f"AWS_DEFAULT_REGION={aws_region}",
            f"OPENCLAW_WORKSPACE={workspace_val}",
            "OPENCLAW_SKIP_ONBOARDING=1",
        ] + openclaw_cmd
        run_env = None
    else:
        cmd = openclaw_cmd
        run_env = env

    logger.info("Invoking openclaw tenant_id=%s cmd=%s", tenant_id, " ".join(cmd[:5]))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10, env=run_env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"openclaw timed out after {timeout}s")

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if stderr:
        for line in stderr.splitlines():
            logger.warning("[openclaw stderr] %s", line)

    if not stdout and stderr:
        json_start_stderr = stderr.find("{")
        if json_start_stderr != -1:
            logger.info("JSON found in stderr (Gateway fallback mode)")
            stdout = stderr[json_start_stderr:]

    if not stdout:
        raise RuntimeError(f"openclaw returned empty output (exit={result.returncode})")

    json_start = stdout.find("{")
    if json_start == -1:
        raise RuntimeError(f"No JSON in openclaw output: {stdout[:200]}")

    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(stdout, json_start)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse openclaw JSON: {e} — output: {stdout[:200]}")

    return data


def _apply_guardrail(text: str, source: str, tenant_id: str) -> str:
    """Apply Bedrock Guardrail to text. Returns blocked message or empty string."""
    try:
        import boto3 as _b3gr

        region = os.environ.get("AWS_REGION", "us-east-1")
        bedrock = _b3gr.client("bedrock-runtime", region_name=region)
        resp = bedrock.apply_guardrail(
            guardrailIdentifier=GUARDRAIL_ID,
            guardrailVersion=GUARDRAIL_VERSION,
            source=source,
            content=[{"text": {"text": text}}],
        )
        action = resp.get("action", "NONE")
        if action in ("GUARDRAIL_INTERVENED",):
            blocked_msg = ""
            for out in resp.get("outputs", []):
                t = out.get("text", "")
                if t:
                    blocked_msg = t
                    break
            if not blocked_msg:
                blocked_msg = "This content was blocked by the guardrail policy."
            logger.info("Guardrail %s BLOCKED source=%s tenant=%s", GUARDRAIL_ID, source, tenant_id)
            return blocked_msg
        return ""
    except Exception as e:
        logger.warning("Guardrail check failed (non-fatal, allowing): %s", e)
        return ""


class AgentCoreHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        logger.info(format, *args)

    def do_GET(self):
        if self.path == "/ping":
            self._respond(200, {"status": "Healthy", "time_of_last_update": int(time.time())})
        elif self.path == "/gateway-dashboard":
            self._handle_gateway_dashboard()
        elif self.path == "/gateway-approve-pairing":
            self._handle_gateway_approve_pairing()
        else:
            self._respond(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/admin/refresh/"):
            emp_id = self.path.split("/admin/refresh/")[1].strip("/")
            if not emp_id:
                self._respond(400, {"error": "emp_id required"})
                return
            evicted = []
            with _assembly_lock:
                to_remove = [t for t in _assembled_tenants if emp_id in t]
                for t in to_remove:
                    _assembled_tenants.discard(t)
                    evicted.append(t)
            logger.info("Admin refresh: evicted %d cached tenants for %s", len(evicted), emp_id)
            self._respond(200, {"refreshed": True, "empId": emp_id, "evicted": evicted})
        elif self.path == "/admin/refresh-all":
            with _assembly_lock:
                count = len(_assembled_tenants)
                _assembled_tenants.clear()
            self._respond(200, {"refreshed": True, "evictedCount": count})
        else:
            self._respond(404, {"error": "not found"})

    def _handle_gateway_dashboard(self):
        try:
            env = os.environ.copy()
            env["HOME"] = os.environ.get("HOME", "/root")
            env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
            result = subprocess.run(
                [OPENCLAW_BIN, "dashboard", "--no-open"],
                capture_output=True,
                text=True,
                timeout=45,
                env=env,
            )
            output = result.stdout + result.stderr
            url_match = re.search(r"(https?://\S+#token=\S+)", output)
            if url_match:
                url = url_match.group(1)
                gw_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
                dash_token_match = re.search(r"#token=([a-f0-9]+)", url)
                dash_token = dash_token_match.group(1) if dash_token_match else ""
                self._respond(
                    200,
                    {
                        "url": url,
                        "gatewayToken": gw_token,
                        "dashboardToken": dash_token,
                        "port": 18789,
                    },
                )
            else:
                self._respond(500, {"error": "Gateway dashboard URL not found"})
        except subprocess.TimeoutExpired:
            self._respond(504, {"error": "Gateway dashboard timed out"})
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _handle_gateway_approve_pairing(self):
        try:
            env = os.environ.copy()
            env["HOME"] = os.environ.get("HOME", "/root")
            env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
            gw_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
            cmd = [OPENCLAW_BIN, "devices", "approve", "--latest", "--json"]
            if gw_token:
                cmd.extend(["--token", gw_token])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
            output = result.stdout + result.stderr
            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout)
                except (json.JSONDecodeError, ValueError):
                    data = {"raw": output[:300]}
                self._respond(200, {"approved": True, "detail": data})
            else:
                self._respond(200, {"approved": False, "reason": output[:300]})
        except subprocess.TimeoutExpired:
            self._respond(504, {"error": "Approve pairing timed out"})
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def do_POST(self):
        if self.path.startswith("/admin/refresh"):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {}
            emp_id = payload.get("emp_id", "")
            if self.path == "/admin/refresh-all" or not emp_id:
                with _assembly_lock:
                    count = len(_assembled_tenants)
                    _assembled_tenants.clear()
                self._respond(200, {"refreshed": True, "evictedCount": count})
            else:
                evicted = []
                with _assembly_lock:
                    to_remove = [t for t in _assembled_tenants if emp_id in t]
                    for t in to_remove:
                        _assembled_tenants.discard(t)
                        evicted.append(t)
                self._respond(200, {"refreshed": True, "empId": emp_id, "evicted": evicted})
            return

        if self.path == "/admin/channels/add":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._respond(400, {"error": "invalid json"})
                return
            channel = payload.get("channel", "")
            if not channel:
                self._respond(400, {"error": "channel required"})
                return
            cmd = [OPENCLAW_BIN, "channels", "add", "--channel", channel]
            for key in ("token", "bot-token", "app-token", "app-id", "app-secret"):
                val = payload.get(key.replace("-", "_"), payload.get(key, ""))
                if val:
                    cmd.extend([f"--{key}", val])
            try:
                env = os.environ.copy()
                env["HOME"] = os.environ.get("HOME", "/root")
                env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                output = result.stdout + result.stderr
                self._respond(
                    200 if result.returncode == 0 else 500,
                    {
                        "success": result.returncode == 0,
                        "channel": channel,
                        "output": output[:500],
                    },
                )
            except Exception as e:
                self._respond(500, {"success": False, "error": str(e)})
            return

        if self.path == "/admin/channels/remove":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._respond(400, {"error": "invalid json"})
                return
            channel = payload.get("channel", "")
            if not channel:
                self._respond(400, {"error": "channel required"})
                return
            try:
                env = os.environ.copy()
                env["HOME"] = os.environ.get("HOME", "/root")
                env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
                result = subprocess.run(
                    [OPENCLAW_BIN, "channels", "remove", "--channel", channel],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env=env,
                )
                self._respond(200, {"success": result.returncode == 0, "channel": channel})
            except Exception as e:
                self._respond(500, {"success": False, "error": str(e)})
            return

        if self.path == "/admin/channels/list":
            try:
                env = os.environ.copy()
                env["HOME"] = os.environ.get("HOME", "/root")
                env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
                result = subprocess.run(
                    [OPENCLAW_BIN, "channels", "list"], capture_output=True, text=True, timeout=15, env=env
                )
                self._respond(200, {"output": result.stdout + result.stderr})
            except Exception as e:
                self._respond(500, {"error": str(e)})
            return

        if self.path != "/invocations":
            self._respond(404, {"error": "not found"})
            return

        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid json"})
            return

        _file_tenant = ""
        try:
            with open("/tmp/tenant_id") as f:
                _file_tenant = f.read().strip()
        except Exception:
            pass

        tenant_id = (
            self.headers.get("X-Amzn-Bedrock-AgentCore-Runtime-Session-Id")
            or self.headers.get("x-amzn-bedrock-agentcore-runtime-session-id")
            or payload.get("runtimeSessionId")
            or payload.get("sessionId")
            or payload.get("tenant_id")
            or _file_tenant
            or "unknown"
        )

        action = payload.get("action", "")
        if action == "warmup":
            self._handle_warmup(tenant_id, payload)
            return
        if action == "cron":
            self._handle_cron(tenant_id, payload)
            return

        message = validate_message(payload.get("prompt") or payload.get("message") or str(payload))
        logger.info("Invocation tenant_id=%s message_len=%d", tenant_id, len(message))
        self._handle_invocation(tenant_id, message, payload)

    def _handle_warmup(self, tenant_id: str, payload: dict):
        try:
            _ensure_workspace_assembled(tenant_id)
            self._respond(200, {"status": "ready"})
        except Exception as e:
            logger.warning("Warmup assembly failed for %s: %s", tenant_id, e)
            self._respond(200, {"status": "initializing"})

    def _handle_cron(self, tenant_id: str, payload: dict):
        message = validate_message(payload.get("prompt") or payload.get("message") or str(payload))
        if not message:
            self._respond(400, {"error": "Missing message in cron payload"})
            return
        logger.info("Cron invocation tenant_id=%s message_len=%d", tenant_id, len(message))
        self._handle_invocation(tenant_id, message, payload)

    def _handle_invocation(self, tenant_id: str, message: str, payload: dict):
        _check_and_refresh_config_version()

        if GUARDRAIL_ID:
            blocked_msg = _apply_guardrail(message, source="INPUT", tenant_id=tenant_id)
            if blocked_msg:
                self._respond(
                    200, {"response": blocked_msg, "status": "guardrail_blocked", "guardrailId": GUARDRAIL_ID}
                )
                return

        # Check session takeover
        stack = os.environ.get("STACK_NAME", "openclaw")
        region = os.environ.get("AWS_REGION", "us-east-1")
        session_key = tenant_id[:40]
        try:
            import boto3 as _b3tk

            ssm_tk = _b3tk.client("ssm", region_name=region)
            admin_param = ssm_tk.get_parameter(Name=f"/openclaw/{stack}/sessions/{session_key}/takeover")
            admin_id = admin_param["Parameter"]["Value"]
            logger.info("Session %s is in takeover by %s", session_key, admin_id)
            self._respond(
                200,
                {
                    "response": "",
                    "status": "takeover",
                    "takenOverBy": admin_id,
                    "message": "Session is being managed by a human admin.",
                },
            )
            return
        except Exception:
            pass

        _ensure_workspace_assembled(tenant_id)

        start_ms = int(time.time() * 1000)
        try:
            timeout = int(payload.get("timeout", 300))
            data = invoke_openclaw(tenant_id, message, timeout=timeout)
            duration_ms = int(time.time() * 1000) - start_ms

            result_block = data.get("result", data)
            payloads = result_block.get("payloads", [])
            response_text = " ".join(p.get("text", "") for p in payloads if p.get("text")).strip()

            if not response_text:
                response_text = result_block.get("text", data.get("text", ""))
            if not response_text:
                response_text = "(no response)"

            if GUARDRAIL_ID:
                blocked_msg = _apply_guardrail(response_text, source="OUTPUT", tenant_id=tenant_id)
                if blocked_msg:
                    self._respond(
                        200, {"response": blocked_msg, "status": "guardrail_blocked", "guardrailId": GUARDRAIL_ID}
                    )
                    return

            try:
                profile = read_permission_profile(tenant_id)
                allowed = profile.get("tools", ["web_search"])
            except Exception:
                allowed = ["web_search"]
            _audit_response(tenant_id, response_text, allowed)

            meta = result_block.get("meta", data.get("meta", {}))
            agent_meta = meta.get("agentMeta", {})
            model = agent_meta.get("model", "unknown")
            usage = agent_meta.get("usage", {})

            log_agent_invocation(tenant_id=tenant_id, tools_used=[], duration_ms=duration_ms, status="success")

            base_id = tenant_id
            parts = tenant_id.split("__")
            if len(parts) >= 3:
                base_id = parts[1]
            elif len(parts) == 2:
                base_id = parts[1]
            try:
                with open("/tmp/base_tenant_id") as f:
                    resolved = f.read().strip()
                    if resolved and resolved != "unknown":
                        base_id = resolved
            except Exception:
                pass

            threading.Thread(
                target=_write_usage_to_dynamodb,
                args=(tenant_id, base_id, usage, model, duration_ms, message),
                daemon=True,
            ).start()

            is_playground = tenant_id.startswith("pgnd__")

            if not is_playground:
                threading.Thread(
                    target=_append_conversation_turn,
                    args=(tenant_id, message, response_text, model, duration_ms),
                    daemon=True,
                ).start()
                threading.Thread(
                    target=_sync_heartbeat_and_memory,
                    args=(base_id,),
                    daemon=True,
                ).start()

            self._respond(
                200,
                {
                    "response": response_text,
                    "status": "success",
                    "model": model,
                    "usage": usage,
                },
            )

        except Exception as e:
            duration_ms = int(time.time() * 1000) - start_ms
            log_agent_invocation(tenant_id=tenant_id, tools_used=[], duration_ms=duration_ms, status="error")
            logger.error("Invocation failed tenant_id=%s error=%s", tenant_id, e)
            self._respond(500, {"error": str(e)})

    def _respond(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    port = int(os.environ.get("PORT", 8080))

    class ThreadedServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedServer(("0.0.0.0", port), AgentCoreHandler)
    logger.info("HTTP server listening on port %d", port)
    logger.info("openclaw binary: %s", OPENCLAW_BIN)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
