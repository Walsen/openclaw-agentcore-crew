"""Security Stack — KMS, Secrets Manager, and Cognito for OpenClaw.

Creates:
  - KMS CMK for encrypting secrets and S3 data at rest
  - Secrets Manager secrets for each channel (Telegram, Slack, WhatsApp, Discord)
  - A webhook verification secret (auto-generated)
  - Cognito User Pool for optional web UI authentication
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_kms as kms,
    aws_secretsmanager as secretsmanager,
    aws_cognito as cognito,
)
from constructs import Construct


class SecurityStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        channels = self.node.try_get_context("channels") or ["telegram"]

        # --- KMS CMK ------------------------------------------------------
        self.kms_key = kms.Key(
            self,
            "MasterKey",
            alias="openclaw/master",
            description="OpenClaw encryption key",
            enable_key_rotation=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # --- Channel secrets (empty — populate before setup scripts) ------
        self.channel_secrets: dict[str, secretsmanager.Secret] = {}
        for channel in channels:
            secret = secretsmanager.Secret(
                self,
                f"{channel.capitalize()}Secret",
                secret_name=f"openclaw/channels/{channel}",
                description=f"OpenClaw {channel} bot credentials",
                encryption_key=self.kms_key,
            )
            self.channel_secrets[channel] = secret

        # --- Webhook verification secret (auto-generated) -----------------
        self.webhook_secret = secretsmanager.Secret(
            self,
            "WebhookSecret",
            secret_name="openclaw/webhook-secret",
            description="HMAC key for webhook signature verification",
            encryption_key=self.kms_key,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64,
                exclude_punctuation=True,
            ),
        )
