"""Token processor Lambda — aggregates usage and enforces budgets.

Called after each AgentCore invocation to record token usage.
Checks daily budgets and can disable users who exceed limits.
"""

import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
ssm_client = boto3.client("ssm")
USAGE_TABLE = os.environ["USAGE_TABLE"]
# Baselines from deploy-time env vars. Live budgets are read from SSM
# (/openclaw/config/*) at invocation, so they can be tuned without a redeploy.
DAILY_TOKEN_BUDGET = int(os.environ.get("DAILY_TOKEN_BUDGET", "1000000"))
DAILY_COST_BUDGET = float(os.environ.get("DAILY_COST_BUDGET_USD", "10"))
TOKEN_TTL_DAYS = int(os.environ.get("TOKEN_TTL_DAYS", "90"))

usage_table = dynamodb.Table(USAGE_TABLE)


# ── Runtime-tunable config (SSM Parameter Store) ──────────────────────────
CONFIG_PREFIX = "/openclaw/config"
CONFIG_TTL = 60  # seconds
_config_cache: dict[str, tuple[str, float]] = {}


def _ssm_config(name: str, default: str) -> str:
    """Return /openclaw/config/<name> from SSM, cached ~CONFIG_TTL seconds.

    Falls back to `default` (the deploy-time env baseline) if the parameter is
    missing or SSM errors, so budget reads never fail the processor.
    """
    now = time.time()
    cached = _config_cache.get(name)
    if cached and now - cached[1] < CONFIG_TTL:
        return cached[0]
    try:
        resp = ssm_client.get_parameter(Name=f"{CONFIG_PREFIX}/{name}")
        value = resp["Parameter"]["Value"]
    except Exception as e:
        logger.warning("SSM config read failed for %s: %s — using default", name, e)
        value = default
    _config_cache[name] = (value, now)
    return value


def handler(event, context):
    """Record token usage and check budgets."""
    try:
        record = event if isinstance(event, dict) else json.loads(event)

        tenant_id = record.get("tenant_id", "unknown")
        input_tokens = record.get("input_tokens", 0)
        output_tokens = record.get("output_tokens", 0)
        model_id = record.get("model_id", "unknown")
        today = time.strftime("%Y-%m-%d")
        ttl = int(time.time()) + (TOKEN_TTL_DAYS * 86400)

        # Live budgets — read from SSM at invocation (tunable without redeploy)
        daily_token_budget = int(_ssm_config("daily-token-budget", str(DAILY_TOKEN_BUDGET)))

        # Write usage record
        usage_table.put_item(
            Item={
                "pk": f"USAGE#{tenant_id}#{today}",
                "sk": f"{int(time.time() * 1000)}",
                "tenant_id": tenant_id,
                "date": today,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "model_id": model_id,
                "ttl": ttl,
            }
        )

        # Check daily budget
        resp = usage_table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"USAGE#{tenant_id}#{today}"},
            Select="SPECIFIC_ATTRIBUTES",
            ProjectionExpression="total_tokens",
        )
        daily_total = sum(item.get("total_tokens", 0) for item in resp.get("Items", []))

        budget_exceeded = daily_total > daily_token_budget
        if budget_exceeded:
            logger.warning(
                "Budget exceeded for %s: %d tokens (limit: %d)",
                tenant_id,
                daily_total,
                daily_token_budget,
            )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "tenant_id": tenant_id,
                    "daily_total": daily_total,
                    "budget_exceeded": budget_exceeded,
                }
            ),
        }

    except Exception:
        logger.exception("Token processor error")
        return {"statusCode": 500, "error": "Internal error"}
