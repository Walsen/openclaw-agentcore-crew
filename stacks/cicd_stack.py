"""CI/CD Stack — GitHub Actions OIDC provider + keyless deploy roles.

Codifies the previously-manual GitHub deploy identity so a NEW AWS account can
be stood up without hand-creating IAM. Creates:

  * (optional) the GitHub Actions OIDC provider
    (token.actions.githubusercontent.com). One provider per account — set
    context `cicd_create_oidc_provider=false` if the account already has one.
  * OpenClawGitHubECRDeploy — assumed by the image repo's workflows
    (deploy-ecr.yml): push to ECR + get/update the AgentCore runtime.
  * OpenClawGitHubCDKDeploy — assumed by the infra repo's workflows
    (cdk-deploy.yml): may ONLY assume the CDK bootstrap roles (cdk-<qualifier>-*),
    so it holds no standing admin; the bootstrap roles do the CloudFormation work.

This stack is OPT-IN: app.py only instantiates it when context
`enable_cicd_stack=true`. The original account already has these resources
created manually, so leave it disabled there to avoid EntityAlreadyExists.

Required context (cdk.json):
  account, region, github_image_repo, github_infra_repo,
  cicd_create_oidc_provider (bool), ecr_repository, cdk_qualifier
"""

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from constructs import Construct

_GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
_GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
_STS_AUDIENCE = "sts.amazonaws.com"


class CicdStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = self.account
        region = self.region
        image_repo = self.node.try_get_context("github_image_repo") or "Walsen/openclaw"
        infra_repo = self.node.try_get_context("github_infra_repo") or "Walsen/openclaw-agentcore-crew"
        ecr_repository = self.node.try_get_context("ecr_repository") or "openclaw-runtime"
        qualifier = self.node.try_get_context("cdk_qualifier") or "hnb659fds"
        create_oidc = self.node.try_get_context("cicd_create_oidc_provider")
        # Default to creating it (new-account case) unless explicitly disabled.
        create_oidc = True if create_oidc is None else bool(create_oidc)

        # --- GitHub OIDC provider -----------------------------------------
        if create_oidc:
            oidc = iam.OpenIdConnectProvider(
                self,
                "GitHubOidcProvider",
                url=_GITHUB_OIDC_URL,
                client_ids=[_STS_AUDIENCE],
            )
        else:
            oidc = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
                self,
                "GitHubOidcProvider",
                f"arn:aws:iam::{account}:oidc-provider/{_GITHUB_OIDC_HOST}",
            )

        def _gh_principal(repo: str) -> iam.OpenIdConnectPrincipal:
            """Federated principal trusting a specific GitHub repo (any ref)."""
            return iam.OpenIdConnectPrincipal(
                oidc,
                conditions={
                    "StringEquals": {f"{_GITHUB_OIDC_HOST}:aud": _STS_AUDIENCE},
                    "StringLike": {f"{_GITHUB_OIDC_HOST}:sub": f"repo:{repo}:*"},
                },
            )

        # --- Image repo role: ECR push + AgentCore runtime update ---------
        ecr_role = iam.Role(
            self,
            "GitHubEcrDeployRole",
            role_name="OpenClawGitHubECRDeploy",
            assumed_by=_gh_principal(image_repo),
            max_session_duration=cdk.Duration.hours(1),
            description=f"GitHub OIDC deploy role for {image_repo} (ECR + AgentCore runtime update)",
        )
        ecr_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrAuth",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        ecr_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrPushPull",
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                ],
                resources=[f"arn:aws:ecr:{region}:{account}:repository/{ecr_repository}"],
            )
        )
        ecr_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreRuntimeUpdate",
                actions=[
                    "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore:UpdateAgentRuntime",
                ],
                resources=[f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/*"],
            )
        )
        # update_agent_runtime re-submits the runtime's execution role -> PassRole,
        # scoped to roles handed to the AgentCore service.
        ecr_role.add_to_policy(
            iam.PolicyStatement(
                sid="PassRuntimeExecutionRole",
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{account}:role/*"],
                conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            )
        )

        # --- Infra repo role: assume CDK bootstrap roles only -------------
        cdk_role = iam.Role(
            self,
            "GitHubCdkDeployRole",
            role_name="OpenClawGitHubCDKDeploy",
            assumed_by=_gh_principal(infra_repo),
            max_session_duration=cdk.Duration.hours(2),
            description=f"GitHub OIDC deploy role for {infra_repo} (assumes cdk-{qualifier}-* bootstrap roles)",
        )
        cdk_role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeCdkBootstrapRoles",
                actions=["sts:AssumeRole"],
                resources=[f"arn:aws:iam::{account}:role/cdk-{qualifier}-*"],
            )
        )
        cdk_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadCdkBootstrapVersion",
                actions=["ssm:GetParameter"],
                resources=[f"arn:aws:ssm:{region}:{account}:parameter/cdk-bootstrap/{qualifier}/version"],
            )
        )

        # --- Outputs (use these to set the GitHub repo variables) ---------
        cdk.CfnOutput(self, "EcrDeployRoleArn", value=ecr_role.role_arn)
        cdk.CfnOutput(self, "CdkDeployRoleArn", value=cdk_role.role_arn)
        if create_oidc:
            cdk.CfnOutput(self, "GitHubOidcProviderArn", value=oidc.open_id_connect_provider_arn)
