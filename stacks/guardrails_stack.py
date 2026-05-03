"""Guardrails Stack — Bedrock Guardrails for content safety.

Deploys a Bedrock Guardrail with:
  - Content filters (hate, insults, sexual, violence, misconduct, prompt attack)
  - PII redaction (ANONYMIZE by default)
  - Topic denial for dangerous topics
  - Word filters for profanity

The guardrail ID is passed to the container as GUARDRAIL_ID env var.
When enable_guardrails is false, this stack still deploys but the ID
is not injected — server.py skips guardrail checks when the var is empty.
"""

import aws_cdk as cdk
from aws_cdk import aws_bedrock as bedrock
from constructs import Construct


class GuardrailsStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        enabled = self.node.try_get_context("enable_guardrails")
        if not enabled:
            self.guardrail_id = ""
            self.guardrail_version = ""
            return

        filter_level = (
            self.node.try_get_context("guardrails_content_filter_level") or "HIGH"
        )
        pii_action = (
            self.node.try_get_context("guardrails_pii_action") or "ANONYMIZE"
        )

        content_filters = [
            {"type": t, "inputStrength": filter_level, "outputStrength": filter_level}
            for t in [
                "HATE",
                "INSULTS",
                "SEXUAL",
                "VIOLENCE",
                "MISCONDUCT",
                "PROMPT_ATTACK",
            ]
        ]

        pii_entities = [
            {"type": e, "action": pii_action}
            for e in [
                "EMAIL",
                "PHONE",
                "NAME",
                "US_SOCIAL_SECURITY_NUMBER",
                "CREDIT_DEBIT_CARD_NUMBER",
                "IP_ADDRESS",
            ]
        ]

        self.guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name="openclaw-guardrail",
            blocked_input_messaging="I can't process that request due to content policy.",
            blocked_outputs_messaging="I can't provide that response due to content policy.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=f["type"],
                        input_strength=f["inputStrength"],
                        output_strength=f["outputStrength"],
                    )
                    for f in content_filters
                ],
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type=e["type"],
                        action=e["action"],
                    )
                    for e in pii_entities
                ],
            ),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="DangerousActivities",
                        definition="Instructions for creating weapons, explosives, drugs, or other dangerous substances",
                        type="DENY",
                    ),
                ],
            ),
            word_policy_config=bedrock.CfnGuardrail.WordPolicyConfigProperty(
                managed_word_lists_config=[
                    bedrock.CfnGuardrail.ManagedWordsConfigProperty(type="PROFANITY"),
                ],
            ),
        )

        self.guardrail_id = self.guardrail.attr_guardrail_id
        self.guardrail_version = "DRAFT"

        cdk.CfnOutput(self, "GuardrailId", value=self.guardrail_id)
