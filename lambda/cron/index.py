"""Cron executor Lambda — runs scheduled tasks via AgentCore.

Invoked by EventBridge Scheduler. Receives the scheduled task payload,
looks up the user session, and invokes AgentCore to execute the task.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
IDENTITY_TABLE = os.environ["IDENTITY_TABLE"]
identity_table = dynamodb.Table(IDENTITY_TABLE)


def handler(event, context):
    """Handle scheduled task execution."""
    try:
        logger.info("Cron event: %s", json.dumps(event))

        tenant_id = event.get("tenant_id")
        task_message = event.get("message", "")
        task_id = event.get("task_id", "unknown")

        if not tenant_id or not task_message:
            logger.error("Missing tenant_id or message in cron event")
            return {"statusCode": 400, "error": "Missing required fields"}

        # Verify user still exists and is active
        resp = identity_table.get_item(
            Key={"pk": f"USER#{tenant_id}", "sk": "PROFILE"}
        )
        user = resp.get("Item")
        if not user or user.get("status") != "active":
            logger.warning("Cron task %s: user %s not found or inactive", task_id, tenant_id)
            return {"statusCode": 404, "error": "User not found"}

        logger.info("Executing cron task %s for tenant %s", task_id, tenant_id)

        # TODO: Invoke AgentCore Runtime with the scheduled message
        # once runtime_id is available from Phase 2 deployment.

        return {"statusCode": 200, "body": json.dumps({"task_id": task_id, "status": "executed"})}

    except Exception:
        logger.exception("Cron executor error")
        return {"statusCode": 500, "error": "Internal error"}
