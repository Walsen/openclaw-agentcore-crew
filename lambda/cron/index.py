"""Cron executor Lambda — runs scheduled tasks via AgentCore.

Invoked by EventBridge Scheduler. Receives the scheduled task payload,
looks up the user session, and invokes AgentCore to execute the task.

Expected event payload:
  {
    "tenant_id": "tg__123456789",
    "message": "Check my email and summarize",
    "task_id": "task-abc123"
  }
"""

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
IDENTITY_TABLE = os.environ["IDENTITY_TABLE"]
RUNTIME_ID = os.environ.get("RUNTIME_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

identity_table = dynamodb.Table(IDENTITY_TABLE)
agentcore_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)


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
        resp = identity_table.get_item(Key={"pk": f"USER#{tenant_id}", "sk": "PROFILE"})
        user = resp.get("Item")
        if not user or user.get("status") != "active":
            logger.warning("Cron task %s: user %s not found or inactive", task_id, tenant_id)
            return {"statusCode": 404, "error": "User not found"}

        if not RUNTIME_ID:
            logger.error("RUNTIME_ID not set — Phase 2 not yet deployed")
            return {"statusCode": 503, "error": "Runtime not configured"}

        logger.info("Executing cron task %s for tenant %s", task_id, tenant_id)

        # Invoke AgentCore Runtime
        resp = agentcore_client.invoke_agent_runtime(
            agentRuntimeId=RUNTIME_ID,
            qualifier="DEFAULT",
            runtimeSessionId=tenant_id,
            payload=json.dumps(
                {
                    "action": "cron",
                    "prompt": task_message,
                    "task_id": task_id,
                }
            ).encode(),
        )
        body_bytes = resp["response"].read()
        data = json.loads(body_bytes)
        logger.info("Cron task %s completed: %s", task_id, data.get("status", "unknown"))

        return {
            "statusCode": 200,
            "body": json.dumps({"task_id": task_id, "status": "executed"}),
        }

    except ClientError as e:
        logger.error("AgentCore invocation failed for cron task %s: %s", task_id, e)
        return {"statusCode": 500, "error": str(e)}
    except Exception:
        logger.exception("Cron executor error")
        return {"statusCode": 500, "error": "Internal error"}
