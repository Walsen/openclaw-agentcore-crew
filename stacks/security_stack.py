"""Security Stack — KMS and Secrets Manager for OpenClaw.

Creates:
  - KMS CMK for encrypting secrets at rest
  - Secrets Manager secrets for each channel (Telegram, Slack, WhatsApp, Discord)
  - A webhook verification secret (auto-generated)
  - A Google OAuth secret for Gmail/Calendar/Drive/Sheets/Docs integration
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
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

        # --- Google OAuth secret ------------------------------------------
        # Stores OAuth 2.0 credentials for Gmail, Calendar, Drive, Sheets,
        # Docs, and Contacts via the gog (gogcli) skill.
        #
        # Expected JSON structure (populated by `just setup-google`):
        # {
        #   "client_id":     "...",
        #   "client_secret": "...",
        #   "refresh_token": "...",
        #   "account":       "you@gmail.com",
        #   "scopes":        ["gmail.modify", "calendar", "drive", ...]
        # }
        self.google_secret = secretsmanager.Secret(
            self,
            "GoogleSecret",
            secret_name="openclaw/google-oauth",
            description="Google OAuth 2.0 credentials for Gmail/Calendar/Drive/Sheets/Docs",
            encryption_key=self.kms_key,
        )

        # Export KMS key ARN so other stacks can import it without creating
        # a cross-stack object reference (which causes dependency cycles)
        cdk.CfnOutput(
            self,
            "KmsKeyArn",
            value=self.kms_key.key_arn,
            export_name="OpenClaw-KmsKeyArn",
        )

        cdk.CfnOutput(
            self,
            "GoogleSecretArn",
            value=self.google_secret.secret_arn,
            description="ARN of the Google OAuth secret — populate with `just setup-google`",
        )
