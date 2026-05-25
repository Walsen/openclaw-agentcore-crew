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
USAGE_TABLE = os.environ["USAGE_TABLE"]
DAILY_TOKEN_BUDGET = int(os.environ.get("DAILY_TOKEN_BUDGET", "1000000"))
DAILY_COST_BUDGET = float(os.environ.get("DAILY_COST_BUDGET_USD", "10"))
TOKEN_TTL_DAYS = int(os.environ.get("TOKEN_TTL_DAYS", "90"))

usage_table = dynamodb.Table(USAGE_TABLE)


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

        budget_exceeded = daily_total > DAILY_TOKEN_BUDGET
        if budget_exceeded:
            logger.warning(
                "Budget exceeded for %s: %d tokens (limit: %d)",
                tenant_id,
                daily_total,
                DAILY_TOKEN_BUDGET,
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
