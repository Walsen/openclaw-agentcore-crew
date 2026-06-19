# Standing up OpenClaw in a new AWS account

This runbook covers the **manual / interactive** steps required to deploy OpenClaw
into a fresh AWS account. Everything not listed here is handled by CDK
(`app.py` / `scripts/cli.py`).

> Account-specific values below use placeholders: `<ACCOUNT>` (12-digit AWS account id),
> `<REGION>` (e.g. `us-east-1`), `<PROFILE>` (your AWS CLI/SSO profile).

---

## 0. Prerequisites
- AWS CLI v2, Node 22+, Python 3.13+, `aws-cdk` (or use the GitHub workflow), `uv`, `gh`.
- AgentCore Runtime is only available in `us-east-1`, `us-west-2`, `eu-west-1`, `ap-northeast-1`.

## 1. AWS account access
```bash
aws configure sso              # or set up the profile
aws sso login --profile <PROFILE>
aws sts get-caller-identity --profile <PROFILE>   # confirm <ACCOUNT>
```
Update `.envrc` (`export AWS_PROFILE=<PROFILE>`) and run `direnv allow`.

## 2. Enable Bedrock models (manual, console)
In the **Bedrock console → Model access**, enable the Claude model(s) referenced by
`default_model_id` in `cdk.json` for `<REGION>`. Deploys succeed without this, but the
agent returns "LLM request failed" until model access is granted.

## 3. Edit `cdk.json` context
Set at minimum:
```jsonc
"account": "<ACCOUNT>",
"region":  "<REGION>",
"aws_profile": "<PROFILE>",        // or "" to use ambient creds
"google_account": "you@example.com",
// runtime_id is filled in AFTER Phase 2 (step 6)
```

## 4. CDK bootstrap (once per account/region)
```bash
cdk bootstrap aws://<ACCOUNT>/<REGION>
```
Creates the `cdk-hnb659fds-*` roles + assets bucket that every deploy relies on.

## 5. Deploy Phase 1 (foundation)
```bash
just deploy-phase1     # VPC, Security, Guardrails, Observability
```
Secrets are created **empty** by the Security stack — populate them next.

## 6. Deploy Phase 2 (AgentCore runtime) — needs the image
```bash
just deploy-phase2-dockerhub   # pulls ffactory/openclaw:latest -> ECR -> creates runtime
```
> First Workspace-enabled deploy MUST go through this `cli.py` path (not the
> GitHub `deploy-ecr.yml`), because it injects `GOG_*` env incl. `GOG_KEYRING_PASSWORD`.
> `deploy-ecr.yml` only *preserves* existing runtime env.

Capture the new `runtime_id` from the output and write it into `cdk.json`
(`"runtime_id": "openclaw_agent-XXXX"`).

## 7. Deploy Phase 3 (application)
```bash
just deploy-phase3     # Router, Cron, TokenMonitoring (creates the OpenClaw DynamoDB app table)
```

## 8. Google Workspace (manual + interactive) — only if using gog
**Google Cloud Console**: create a project, enable the APIs (Gmail/Calendar/Drive/
Sheets/Docs/Contacts), configure the OAuth consent screen, create an **OAuth client**
(client id/secret). Add yourself as a **test user** OR publish the app — apps left in
"Testing" expire refresh tokens after 7 days.

Then mint the token (opens a browser):
```bash
just setup-google                                  # initial consent
# or to (re)set scopes incl. gmail.modify for email move/trash:
just refresh-google-token you@example.com full
```
This populates the `openclaw/google-oauth` secret (and auto-generates `keyring_password`).
Re-run a Phase 2 env refresh so the runtime picks up the credentials/scopes.

## 9. Channels (manual + interactive)
```bash
# Telegram: create a bot via @BotFather, then:
just setup-telegram        # stores token, registers webhook, prompts for your TG user id
# (similar: just setup-slack / setup-whatsapp / setup-discord)
```

## 10. Allowlist + permission grants (data, manual)
```bash
just add-user telegram:<YOUR_TG_USER_ID> "Your Name"
```
Grant Workspace (`gog`) capability to a tenant by writing permission records into the
`OpenClaw` app table (PK=`ORG#acme`):
```bash
# Position with the capability
aws dynamodb put-item --region <REGION> --table-name OpenClaw --item '{
  "PK":{"S":"ORG#acme"},"SK":{"S":"POS#pos-gog-user"},
  "name":{"S":"gog-user"},"toolAllowlist":{"L":[{"S":"web_search"},{"S":"gog"}]}}'
# Employee -> position (base id = the channel user id, e.g. Telegram numeric id)
aws dynamodb put-item --region <REGION> --table-name OpenClaw --item '{
  "PK":{"S":"ORG#acme"},"SK":{"S":"EMP#<BASE_ID>"},"positionId":{"S":"pos-gog-user"}}'
```

## 11. CI/CD identity (now codified — deploy the Cicd stack)
The GitHub OIDC provider + deploy roles are defined in `stacks/cicd_stack.py`.
For a new account:
```bash
# In cdk.json context add:
#   "enable_cicd_stack": true
#   "cicd_create_oidc_provider": true     // false if the account already has a GitHub OIDC provider
#   "github_image_repo": "Walsen/openclaw"
#   "github_infra_repo": "Walsen/openclaw-agentcore-crew"
cdk deploy OpenClawCicd
```
Read the stack outputs and set the **GitHub repo variables**:

| Repo | Variable | Value |
|---|---|---|
| `Walsen/openclaw` | `AWS_ROLE_ARN` | `EcrDeployRoleArn` output |
| `Walsen/openclaw` | `AWS_REGION` | `<REGION>` |
| `Walsen/openclaw` | `ECR_REPOSITORY` | `openclaw-runtime` |
| `Walsen/openclaw` | `AGENT_RUNTIME_ID` | the Phase-2 `runtime_id` |
| `openclaw-agentcore-crew` | `AWS_CDK_ROLE_ARN` | `CdkDeployRoleArn` output |
| `openclaw-agentcore-crew` | `AWS_REGION` | `<REGION>` |

```bash
gh variable set AWS_ROLE_ARN --repo Walsen/openclaw --body "<EcrDeployRoleArn>"
# ...repeat for each variable above
```

> The **original** account already has these IAM resources created by hand — do NOT
> deploy `OpenClawCicd` there (leave `enable_cicd_stack` unset) or it will clash
> (`EntityAlreadyExists`).

## 12. Verify
- `curl`/Telegram smoke test (cold start ~10–15s).
- `just gog-logs --since 30m` shows `refresh token exchange succeeded`.
- "save a note to my Drive" / "archive emails from <sender>".

---

## Still NOT codified (intentional manual steps)
- Bedrock **model access** enablement (console only).
- **Google Cloud** project/OAuth setup + browser consent.
- **Channel** bot creation (BotFather etc.) + webhook registration.
- Populating **secrets** and seeding **permission/allowlist** data.
- Setting **GitHub repo variables** (ARNs are known only after deploy).
- **Branch protection** on `main` (per repo; see repo settings / the protection API).
