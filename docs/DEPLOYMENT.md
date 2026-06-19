# Deployment Guide

How OpenClaw is built, deployed, and operated across its two repositories. This
is the canonical end-to-end guide. For standing up a **brand-new AWS account**
from scratch, follow [NEW-ACCOUNT.md](./NEW-ACCOUNT.md) — it lists every manual
step in order. This document explains the day-to-day deploy paths and what is
automated vs. manual.

---

## The two repositories

| Repo | Builds | Deploys |
|---|---|---|
| [`Walsen/openclaw`](https://github.com/Walsen/openclaw) | the agent **Docker image** (`ffactory/openclaw:latest`) | nothing — image only |
| [`Walsen/openclaw-agentcore-crew`](https://github.com/Walsen/openclaw-agentcore-crew) | the **AWS infrastructure** (CDK) | everything that runs the image |

The image repo publishes to Docker Hub and ECR; the infra repo pulls/uses that
image to create the AgentCore Runtime and supporting AWS resources.

```
Walsen/openclaw ──build──> ffactory/openclaw:latest ──> ECR ──> AgentCore Runtime
                                                                      ▲
Walsen/openclaw-agentcore-crew ──CDK──> VPC, Lambdas, DynamoDB, ──────┘
                                        Guardrails, SSM, IAM, …
```

---

## What's automated vs. manual

| Concern | Automated by | Manual? |
|---|---|---|
| VPC, Security, Guardrails, Observability | CDK (Phase 1) | no |
| AgentCore Runtime create/update + env injection | `scripts/cli.py` (Phase 2) | no |
| Router, Cron, TokenMonitoring | CDK (Phase 3) | no |
| `runtime_id` propagation | SSM `/openclaw/runtime-id` (written by Phase 2) | no |
| Runtime-tunable config | SSM `/openclaw/config/*` (seeded by Phase 3) | no |
| GitHub OIDC provider + deploy roles | `stacks/cicd_stack.py` (opt-in) | no¹ |
| Image build + push | GitHub Actions (`deploy-ecr.yml`) / `publish.yml` | no |
| Bedrock model access | — | **yes** (console) |
| Google Cloud project + OAuth consent | — | **yes** (console + browser) |
| Channel bot creation (BotFather, etc.) | — | **yes** |
| Secrets population & user allowlist | `just setup-*` / `just add-user` | **interactive** |
| GitHub repo variables (role ARNs) | — | **yes** (known only post-deploy) |
| Branch protection on `main` | — | **yes** (per repo) |

¹ The original account's IAM was created by hand before `cicd_stack.py` existed —
do **not** deploy `OpenClawCicd` there. New accounts deploy it.

---

## Deploy paths

There are three ways changes reach AWS. Pick by what changed.

### 1. Infra (CDK stacks) — GitHub Actions, on demand

CDK changes deploy through `.github/workflows/cdk-deploy.yml` using GitHub OIDC
(no local CDK needed — the devbox `cdk` is intentionally pinned older).

- **On a PR**: the workflow runs `cdk diff` and posts the plan.
- **On demand**: trigger a real deploy of one stack.

```bash
gh workflow run cdk-deploy.yml -f stack=OpenClawRouter
gh workflow run cdk-deploy.yml -f stack=OpenClawTokenMonitoring
# watch it
gh run watch <run-id> --exit-status
```

Local synth validation (no deploy), useful before opening a PR:

```bash
CDK_OUTDIR=cdk.out CDK_DEFAULT_ACCOUNT=<ACCOUNT> CDK_DEFAULT_REGION=<REGION> \
  .venv/bin/python app.py
```

### 2. Agent image — GitHub Actions

A merge to `main` on `Walsen/openclaw` publishes `ffactory/openclaw:latest`
(`publish.yml`). To roll that image onto the runtime, `deploy-ecr.yml` builds and
pushes to ECR and updates the AgentCore Runtime (preserving existing env).

> `deploy-ecr.yml` only **preserves** runtime env — it does not inject new
> `GOG_*`/secret env. The first Workspace-enabled deploy must go through the
> `cli.py` Phase 2 path (below), which injects `GOG_KEYRING_PASSWORD` and the
> Google account env.

### 3. Runtime env / Google credentials — `scripts/cli.py` (no Docker)

Phase 2 and the Google flows run locally via `just`/`cli.py` against the AWS
control plane (boto3) — they do **not** require a local Docker daemon for env-only
updates.

```bash
just deploy-phase2            # default: build local image (needs Docker)
just deploy-phase2-dockerhub  # pull ffactory/openclaw:latest -> ECR -> runtime
```

Adding or refreshing a Google account re-injects credentials onto the running
runtime **without** rebuilding the image (image-preserving env update):

```bash
just setup-google                              # add/update an account, then auto-injects
just refresh-google-token you@example.com full # re-mint token + re-inject
```

---

## Full deploy (phases)

Phases are idempotent and can run together (`just deploy`) or individually.

```bash
just deploy-phase1   # VPC, Security (empty secrets), Guardrails, Observability
just deploy-phase2   # AgentCore Runtime: image -> ECR -> create/update runtime
                     #   writes /openclaw/runtime-id to SSM
just deploy-phase3   # Router + API GW, Cron, TokenMonitoring
                     #   seeds /openclaw/config/* (create-if-absent)
```

Ordering matters on a fresh account: secrets are created **empty** in Phase 1, so
populate Google/channel secrets before the runtime needs them (see NEW-ACCOUNT.md
steps 6–10).

---

## Runtime-tunable configuration (SSM)

Operational knobs live in SSM so they can change **without a redeploy**. The
Lambdas read them at invocation (≈60s in-process cache, env-var fallback).

| Parameter | Used by | Default |
|---|---|---|
| `/openclaw/config/max-users` | Router (registration cap) | `10` |
| `/openclaw/config/registration-open` | Router (self-registration) | `false` |
| `/openclaw/config/daily-token-budget` | Token processor | `1000000` |
| `/openclaw/config/daily-cost-budget-usd` | Token processor | `10` |

Change a value live (takes effect within ~a minute, no deploy):

```bash
aws ssm put-parameter --overwrite --type String \
  --name /openclaw/config/registration-open --value true \
  --profile <PROFILE> --region <REGION>
```

Phase 3 seeds these **create-if-absent** (`Overwrite=False`), so a later
`cdk deploy` never clobbers an operator's change. CDK only grants the Lambda
roles read access on `/openclaw/config/*`; it does not manage the values.

Also resolved from SSM: `/openclaw/runtime-id` (written by Phase 2). The Router
and Cron stacks read it at deploy time, so `runtime_id` need not be committed to
`cdk.json` per account (a `cdk.json` value still overrides if present).

### Not runtime-tunable (still needs a deploy)

- `default_model_id` — container env (would need `server.py` to read SSM).
- Session idle/max-lifetime timeouts — set on the runtime in Phase 2.
- Lambda memory/timeout — CloudFormation-level (CDK).

---

## Image update workflow

1. Merge a change to `main` on `Walsen/openclaw` → `publish.yml` pushes
   `ffactory/openclaw:latest` to Docker Hub.
2. Run `deploy-ecr.yml` (GitHub Actions) to push to ECR and update the runtime,
   **or** `just deploy-phase2-dockerhub` locally.
3. New sessions use the new image; existing sessions finish on the old one until
   they idle-terminate. To force a clean switch, recycle warm sessions with the
   `bedrock-agentcore` `StopRuntimeSession` API.

---

## Operations

```bash
just status            # stack status
just outputs           # CloudFormation outputs
just logs-router       # tail Router Lambda
just logs-cron         # tail Cron Lambda
just gog-logs --since 30m   # AgentCore runtime logs filtered for gog/Google init
```

Smoke test after a deploy: message the bot (cold start ~10–15s) and confirm a
reply. For Workspace, `just gog-logs` should show `refresh token exchange
succeeded`, then try "save a note to my Drive" or "archive emails from <sender>".

---

## CI required checks & branch protection

`main` is branch-protected on **both** repos: PRs only, no direct/force push or
delete, with required status checks. Admins can override but the workflow is to
go through PRs.

| Repo | Required checks |
|---|---|
| `Walsen/openclaw` | lint, test, build, security, secrets |
| `Walsen/openclaw-agentcore-crew` | lint, synth, security, secrets |

---

## Environment gotchas

- **`cdk` CLI** in devbox is intentionally older than the app requires — use the
  `cdk-deploy.yml` workflow for real deploys, or `python app.py` for local synth.
- **No local Docker** on some machines — image builds go through GitHub Actions;
  env-only runtime updates (Phase 2 control-plane, Google re-inject) work locally.
- **`ruff`** is run via `uv tool run ruff` (not on PATH directly).
- **SSO tokens** expire frequently — `aws sso login --profile <PROFILE>`.
- **`git`** may be shadowed inside the repo (devbox) — use `/usr/bin/git` if needed.
