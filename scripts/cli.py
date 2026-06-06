#!/usr/bin/env python3
"""OpenClaw AgentCore — Operations CLI

All AWS interactions go through boto3. Structured logging via rich.
SSO profile is read from cdk.json context.aws_profile.

Usage:
    python scripts/cli.py deploy [--phase 1|2|3]
    python scripts/cli.py teardown [--force] [--dry-run]
    python scripts/cli.py setup telegram|slack|whatsapp|discord
    python scripts/cli.py users list
    python scripts/cli.py users add <channel:id> [--name "Alice"]
    python scripts/cli.py users remove <channel:id>
    python scripts/cli.py outputs
    python scripts/cli.py logs router|cron [--follow]
    python scripts/cli.py status
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

import boto3
import botocore.exceptions
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
log = logging.getLogger("openclaw")
console = Console()

# ── Config ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent


def load_config() -> dict:
    """Load cdk.json context."""
    cdk_json = PROJECT_ROOT / "cdk.json"
    with open(cdk_json) as f:
        return json.load(f)["context"]


def save_config_value(key: str, value: str) -> None:
    """Write a single context key back to cdk.json."""
    cdk_json = PROJECT_ROOT / "cdk.json"
    with open(cdk_json) as f:
        data = json.load(f)
    data["context"][key] = value
    with open(cdk_json, "w") as f:
        json.dump(data, f, indent=2)
    log.info("cdk.json updated: %s = %s", key, value)


def get_boto_session(config: dict) -> boto3.Session:
    """Create a boto3 session using SSO profile from cdk.json.

    If AWS_ACCESS_KEY_ID is already set in the environment (e.g. CI),
    skip the named profile and use the ambient credentials directly.
    """
    region = config.get("region", "us-east-1")
    # In CI, AWS_ACCESS_KEY_ID is set — use ambient credentials directly
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        return boto3.Session(region_name=region)
    # Prefer AWS_PROFILE env var (set in .envrc) over cdk.json value
    profile = os.environ.get("AWS_PROFILE") or config.get("aws_profile", "")
    if profile and profile not in ("None", "REPLACE_WITH_YOUR_SSO_PROFILE_NAME"):
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def verify_credentials(session: boto3.Session) -> str:
    """Verify AWS credentials are active. Returns account ID."""
    try:
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        account = identity["Account"]
        arn = identity["Arn"]
        log.info("AWS identity: %s (account: %s)", arn, account)
        return account
    except botocore.exceptions.NoCredentialsError:
        console.print("[red]✗ No AWS credentials found.[/red]")
        config = load_config()
        profile = config.get("aws_profile", "")
        if profile:
            console.print(f"  Run: [bold]aws sso login --profile {profile}[/bold]")
        else:
            console.print("  Set [bold]aws_profile[/bold] in cdk.json, then run: aws sso login --profile <name>")
        sys.exit(1)
    except botocore.exceptions.ClientError as e:
        if "ExpiredToken" in str(e) or "InvalidClientTokenId" in str(e):
            console.print("[red]✗ SSO session expired.[/red]")
            config = load_config()
            profile = config.get("aws_profile", "")
            console.print(f"  Run: [bold]aws sso login --profile {profile or '<profile>'}[/bold]")
            sys.exit(1)
        raise


def get_stack_output(cfn: boto3.client, stack_name: str, output_key: str) -> Optional[str]:
    """Get a specific output value from a CloudFormation stack."""
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        outputs = resp["Stacks"][0].get("Outputs", [])
        for o in outputs:
            if o["OutputKey"] == output_key:
                return o["OutputValue"]
        return None
    except botocore.exceptions.ClientError:
        return None


def stack_exists(cfn: boto3.client, stack_name: str) -> bool:
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        status = resp["Stacks"][0]["StackStatus"]
        return not status.endswith("_FAILED") and status != "DELETE_COMPLETE"
    except Exception:
        return False


# ── CDK helpers ───────────────────────────────────────────────────────────


def run_cdk(args: list[str], config: dict, dry_run: bool = False) -> None:
    """Run a CDK command with the correct profile and region."""
    region = config.get("region", "us-east-1")
    env = os.environ.copy()
    env["AWS_DEFAULT_REGION"] = region
    env["JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION"] = "1"
    env["VIRTUAL_ENV"] = str(PROJECT_ROOT / ".venv")
    env["PATH"] = str(PROJECT_ROOT / ".venv" / "bin") + os.pathsep + env.get("PATH", "")
    # Use AWS_PROFILE from environment (set by .envrc) if not already set.
    # Skip entirely when ambient credentials are present (CI).
    if not env.get("AWS_ACCESS_KEY_ID") and not env.get("AWS_PROFILE"):
        profile = config.get("aws_profile", "")
        if profile and profile not in ("None", "REPLACE_WITH_YOUR_SSO_PROFILE_NAME"):
            env["AWS_PROFILE"] = profile

    cmd = ["cdk"] + args
    if dry_run:
        log.info("[dry-run] Would run: %s", " ".join(cmd))
        return

    log.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, env=env, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        console.print(f"[red]✗ CDK command failed (exit {result.returncode})[/red]")
        sys.exit(result.returncode)


# ── Deploy ────────────────────────────────────────────────────────────────


def cmd_deploy(args: argparse.Namespace) -> None:
    config = load_config()
    session = get_boto_session(config)
    account = verify_credentials(session)
    region = config["region"]
    prefix = config.get("stack_prefix", "OpenClaw")
    phase = args.phase  # None = all
    use_local = getattr(args, "local", False)

    console.print(
        Panel(
            f"[bold]OpenClaw AgentCore Deployment[/bold]\n"
            f"Account: {account}\n"
            f"Region:  {region}\n"
            f"Image:   {'local build (openclaw/)' if use_local else config['docker_image']}\n"
            f"Profile: {config.get('aws_profile', '(default)')}",
            border_style="blue",
        )
    )

    if phase in (None, "1"):
        _deploy_phase1(config, prefix)
    if phase in (None, "2"):
        _deploy_phase2(config, session, account, region, prefix, use_local=use_local)
    if phase in (None, "3"):
        _deploy_phase3(config, prefix)


def _deploy_phase1(config: dict, prefix: str) -> None:
    console.print("\n[bold cyan]━━━ Phase 1: Foundation Stacks ━━━[/bold cyan]")
    stacks = [f"{prefix}Vpc", f"{prefix}Security", f"{prefix}Guardrails", f"{prefix}Observability"]
    run_cdk(["deploy"] + stacks + ["--require-approval", "never"], config)
    console.print("[green]✓ Phase 1 complete[/green]")


def _deploy_phase2(
    config: dict, session: boto3.Session, account: str, region: str, prefix: str, use_local: bool = False
) -> None:
    console.print("\n[bold cyan]━━━ Phase 2: AgentCore Runtime ━━━[/bold cyan]")
    cfn = session.client("cloudformation")

    # Ensure AgentCore stack is deployed first (needed for execution role ARN)
    agentcore_stack = f"{prefix}AgentCore"
    if not stack_exists(cfn, agentcore_stack):
        log.info("%s not yet deployed — deploying it first", agentcore_stack)
        run_cdk(["deploy", agentcore_stack, "--require-approval", "never"], config)

    role_arn = get_stack_output(cfn, agentcore_stack, "ExecutionRoleArn")
    if not role_arn:
        console.print(f"[red]✗ Could not read ExecutionRoleArn from {agentcore_stack}[/red]")
        sys.exit(1)

    ecr_repo = f"{account}.dkr.ecr.{region}.amazonaws.com/openclaw-runtime"
    docker_image = config["docker_image"]

    # Create ECR repo
    ecr = session.client("ecr")
    try:
        ecr.describe_repositories(repositoryNames=["openclaw-runtime"])
        log.info("ECR repository already exists")
    except ecr.exceptions.RepositoryNotFoundException:
        log.info("Creating ECR repository: openclaw-runtime")
        ecr.create_repository(
            repositoryName="openclaw-runtime",
            imageScanningConfiguration={"scanOnPush": True},
        )
        console.print("[green]✓ ECR repository created[/green]")

    # Docker login to ECR
    log.info("Authenticating Docker with ECR...")
    token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    import base64

    username, password = base64.b64decode(token).decode().split(":", 1)
    _run_subprocess(["docker", "login", "--username", username, "--password-stdin", ecr_repo], input_text=password)

    if use_local:
        # Build from local openclaw/ directory
        openclaw_dir = PROJECT_ROOT.parent / "openclaw"
        if not openclaw_dir.exists():
            console.print(f"[red]✗ openclaw/ directory not found at {openclaw_dir}[/red]")
            sys.exit(1)
        log.info("Building image from %s (linux/arm64)...", openclaw_dir)
        _run_subprocess(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/arm64",
                "--load",
                "-t",
                f"{ecr_repo}:latest",
                str(openclaw_dir),
            ]
        )
        console.print("[green]✓ Image built from local source[/green]")
    else:
        # Pull from Docker Hub directly — credential pre-injection is now in entrypoint.sh
        log.info("Pulling %s (linux/arm64)...", docker_image)
        _run_subprocess(["docker", "pull", "--platform", "linux/arm64", docker_image])
        log.info("Tagging for ECR...")
        _run_subprocess(["docker", "tag", docker_image, f"{ecr_repo}:latest"])

    log.info("Pushing to ECR...")
    _run_subprocess(["docker", "push", f"{ecr_repo}:latest"])
    console.print("[green]✓ Image pushed to ECR[/green]")

    # Build environment variables for the container
    agentcore_env = {
        "S3_BUCKET": f"{config['stack_prefix'].lower()}-workspaces-{account}-{region}",
        "STACK_NAME": config["stack_prefix"],
        "BEDROCK_MODEL_ID": config["default_model_id"],
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        # Skip SSM/DynamoDB calls that require extra permissions and slow startup.
        # Workspace assembly happens on first invocation via server.py instead.
        "OPENCLAW_SKIP_ONBOARDING": "1",
        "OPENCLAW_SKIP_CRON": "1",
    }
    # Add guardrail ID if available from deployed stack
    cfn_client = session.client("cloudformation")
    guardrail_id = get_stack_output(cfn_client, f"{prefix}Guardrails", "GuardrailId") or ""
    if guardrail_id:
        agentcore_env["GUARDRAIL_ID"] = guardrail_id
        agentcore_env["GUARDRAIL_VERSION"] = "DRAFT"

    # Add Google OAuth env vars for all configured accounts.
    #
    # Secret schema (multi-account):
    # {
    #   "accounts": {
    #     "you@gmail.com":   { "client_id", "client_secret", "refresh_token",
    #                          "scopes", "scope_level", "label" },
    #     "you@work.com":    { ... }
    #   },
    #   "default_account": "you@gmail.com"
    # }
    #
    # The gog skill reads per-account env vars:
    #   GOG_ACCOUNT_<SAFE_EMAIL>_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN
    # where SAFE_EMAIL is the address with @ and . replaced by _ (uppercased).
    # GOG_ACCOUNTS lists all configured addresses (comma-separated).
    # GOG_DEFAULT_ACCOUNT is the one used when no account is specified.
    google_secret_arn = f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/google-oauth"
    sm = session.client("secretsmanager")
    try:
        google_secret_raw = sm.get_secret_value(SecretId="openclaw/google-oauth")["SecretString"]
        google_store = json.loads(google_secret_raw) if google_secret_raw else {}
        accounts = google_store.get("accounts", {})

        # Back-compat: single-account format written by older setup-google
        if not accounts and google_store.get("refresh_token"):
            email = google_store.get("account", "")
            accounts = {email: google_store}
            log.info("Migrating single-account Google secret to multi-account format")

        if accounts:
            agentcore_env["GOG_CREDENTIALS_SECRET_ARN"] = google_secret_arn
            agentcore_env["GOG_ACCOUNTS"] = ",".join(accounts.keys())
            agentcore_env["GOG_DEFAULT_ACCOUNT"] = google_store.get("default_account", next(iter(accounts)))
            # gogcli stores the refresh token in a keyring. In the headless
            # AgentCore container there is no OS keyring, so the runtime uses the
            # encrypted FILE backend, which needs a password. Reuse a stable
            # password stored in the secret, generating one on first deploy.
            keyring_password = google_store.get("keyring_password", "")
            if not keyring_password:
                import secrets as _secrets

                keyring_password = _secrets.token_urlsafe(32)
                google_store["keyring_password"] = keyring_password
                sm.put_secret_value(
                    SecretId="openclaw/google-oauth",
                    SecretString=json.dumps(google_store, indent=2),
                )
                log.info("Generated and stored a new gog keyring password in the secret")
            agentcore_env["GOG_KEYRING_BACKEND"] = "file"
            agentcore_env["GOG_KEYRING_PASSWORD"] = keyring_password
            for email, creds in accounts.items():
                safe = email.upper().replace("@", "_AT_").replace(".", "_")
                agentcore_env[f"GOG_ACCOUNT_{safe}_CLIENT_ID"] = creds.get("client_id", "")
                agentcore_env[f"GOG_ACCOUNT_{safe}_CLIENT_SECRET"] = creds.get("client_secret", "")
                agentcore_env[f"GOG_ACCOUNT_{safe}_REFRESH_TOKEN"] = creds.get("refresh_token", "")
                agentcore_env[f"GOG_ACCOUNT_{safe}_SCOPES"] = ",".join(creds.get("scopes", []))
                agentcore_env[f"GOG_ACCOUNT_{safe}_LABEL"] = creds.get("label", email)
            log.info(
                "Google OAuth: injecting %d account(s): %s",
                len(accounts),
                ", ".join(accounts.keys()),
            )
        else:
            log.info("Google OAuth secret has no accounts — run `just setup-google`")
    except sm.exceptions.ResourceNotFoundException:
        log.info("Google OAuth secret not yet created — run `just setup-google` after Phase 1")
    except json.JSONDecodeError:
        log.info("Google OAuth secret is empty — run `just setup-google` to populate")

    # Deploy via boto3 bedrock-agentcore-control (no separate CLI needed)
    log.info("Deploying AgentCore Runtime via boto3 bedrock-agentcore-control...")
    agentcore_control = session.client("bedrock-agentcore-control", region_name=region)

    existing_runtime_id = config.get("runtime_id", "")
    if existing_runtime_id and existing_runtime_id not in ("", "null", "REPLACE_WITH_RUNTIME_ID"):
        # Update existing runtime with new image
        log.info("Updating existing runtime: %s", existing_runtime_id)
        agentcore_control.update_agent_runtime(
            agentRuntimeId=existing_runtime_id,
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": f"{ecr_repo}:latest"}},
            roleArn=role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            lifecycleConfiguration={
                "idleRuntimeSessionTimeout": config["session_idle_timeout"],
                "maxLifetime": config["session_max_lifetime"],
            },
            environmentVariables=agentcore_env,
        )
        console.print(
            Panel(
                f"[bold green]✓ Phase 2 complete![/bold green]\n\n"
                f"Runtime updated: [bold]{existing_runtime_id}[/bold]\n"
                "New sessions will use the updated image automatically.\n\n"
                "Run [bold]just deploy-phase3[/bold] to continue.",
                border_style="green",
            )
        )
    else:
        # Create new runtime
        resp = agentcore_control.create_agent_runtime(
            agentRuntimeName="openclaw_agent",
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": f"{ecr_repo}:latest"}},
            roleArn=role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            lifecycleConfiguration={
                "idleRuntimeSessionTimeout": config["session_idle_timeout"],
                "maxLifetime": config["session_max_lifetime"],
            },
            environmentVariables=agentcore_env,
        )
        new_runtime_id = resp["agentRuntimeId"]
        console.print(f"[green]✓ AgentCore Runtime created: {new_runtime_id}[/green]")

        # Poll until READY (up to 5 minutes)
        log.info("Waiting for runtime to become READY...")
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True
        ) as progress:
            task = progress.add_task("Waiting for runtime...", total=None)
            for _ in range(60):
                time.sleep(5)
                status_resp = agentcore_control.get_agent_runtime(agentRuntimeId=new_runtime_id)
                status = status_resp.get("status", "")
                progress.update(task, description=f"Runtime status: {status}")
                if status == "READY":
                    break
                if "FAILED" in status:
                    reason = status_resp.get("failureReason", "unknown")
                    console.print(f"[red]✗ Runtime creation failed ({status}): {reason}[/red]")
                    sys.exit(1)

        # Automatically save runtime_id to cdk.json — no manual step needed
        save_config_value("runtime_id", new_runtime_id)
        console.print(
            Panel(
                f"[bold green]✓ Phase 2 complete![/bold green]\n\n"
                f"Runtime ID: [bold]{new_runtime_id}[/bold]\n"
                "Saved to cdk.json automatically.\n\n"
                "Run [bold]just deploy-phase3[/bold] to continue.",
                border_style="green",
            )
        )


def _deploy_phase3(config: dict, prefix: str) -> None:
    console.print("\n[bold cyan]━━━ Phase 3: Application Stacks ━━━[/bold cyan]")

    runtime_id = config.get("runtime_id", "")
    if not runtime_id or runtime_id == "REPLACE_WITH_RUNTIME_ID":
        console.print("[red]✗ runtime_id not set in cdk.json.[/red]")
        console.print("  Complete Phase 2 first and add the runtime_id.")
        sys.exit(1)

    stacks = [
        f"{prefix}AgentCore",
        f"{prefix}Router",
        f"{prefix}Cron",
        f"{prefix}TokenMonitoring",
    ]
    run_cdk(["deploy"] + stacks + ["--require-approval", "never"], config)
    console.print("[green]✓ Phase 3 complete[/green]")

    # Print API URL
    session = get_boto_session(config)
    cfn = session.client("cloudformation")
    api_url = get_stack_output(cfn, f"{prefix}Router", "ApiUrl")
    if api_url:
        console.print(
            Panel(
                f"[bold green]Deployment Complete![/bold green]\n\n"
                f"API URL: [bold]{api_url}[/bold]\n\n"
                "Next steps:\n"
                "  1. Run: [bold]just setup-telegram[/bold] (and other channels)\n"
                "  2. Run: [bold]just add-user telegram:YOUR_ID 'Your Name'[/bold]",
                border_style="green",
            )
        )


# ── Teardown ──────────────────────────────────────────────────────────────


def cmd_teardown(args: argparse.Namespace) -> None:
    config = load_config()
    session = get_boto_session(config)
    account = verify_credentials(session)
    region = config["region"]
    prefix = config.get("stack_prefix", "OpenClaw")
    force = args.force
    dry_run = args.dry_run

    console.print(
        Panel(
            "[bold red]OpenClaw — Full Teardown[/bold red]\n"
            + ("[bold yellow]MODE: DRY RUN — nothing will be deleted[/bold yellow]\n" if dry_run else "")
            + f"Region:  {region}\n"
            f"Account: {account}",
            border_style="red",
        )
    )

    if not force and not dry_run:
        console.print("[red]This will permanently delete all OpenClaw AWS resources.[/red]")
        confirm_word = Prompt.ask("Type [bold]delete[/bold] to confirm")
        if confirm_word != "delete":
            console.print("Aborted.")
            return

    cfn = session.client("cloudformation")

    # Step 1: AgentCore Runtime
    _teardown_agentcore_runtime(config, session, region, dry_run, force)

    # Step 2: CDK Phase 3 stacks
    phase3 = [f"{prefix}TokenMonitoring", f"{prefix}Cron", f"{prefix}Router", f"{prefix}AgentCore"]
    _teardown_cdk_stacks("Phase 3", phase3, config, cfn, dry_run, force)

    # Step 3: CDK Phase 1 stacks
    phase1 = [f"{prefix}Observability", f"{prefix}Guardrails", f"{prefix}Security", f"{prefix}Vpc"]
    _teardown_cdk_stacks("Phase 1", phase1, config, cfn, dry_run, force)

    # Step 4: S3 bucket
    bucket_name = f"{prefix.lower()}-workspaces-{account}-{region}"
    _teardown_s3_bucket(session, bucket_name, region, dry_run, force)

    # Step 5: ECR repository
    _teardown_ecr(session, region, dry_run, force)

    # Step 6: KMS key
    _teardown_kms(session, region, dry_run, force)

    # Step 7: CloudWatch log groups
    _teardown_log_groups(session, region, dry_run, force)

    # Step 8: Telegram webhook
    _teardown_telegram_webhook(session, region, dry_run, force)

    console.print(
        Panel(
            "[bold]Teardown Complete[/bold]\n\n"
            "[yellow]Resources NOT deleted (manual cleanup needed):[/yellow]\n"
            "• Secrets Manager secrets (contain bot tokens)\n"
            f"  aws secretsmanager list-secrets --region {region}\n"
            "• DynamoDB identity table: openclaw-identity\n"
            "• CDK bootstrap stack: CDKToolkit",
            border_style="yellow",
        )
    )


def _teardown_agentcore_runtime(config, session, region, dry_run, force):
    console.print("\n[bold]━━━ Step 1: AgentCore Runtime ━━━[/bold]")
    runtime_id = config.get("runtime_id", "")
    if not runtime_id or runtime_id in ("null", ""):
        log.info("No runtime_id in cdk.json — skipping")
        return

    if not force and not Confirm.ask(f"Delete AgentCore Runtime: {runtime_id}?"):
        return

    client = session.client("bedrock-agentcore", region_name=region)
    try:
        endpoints = client.list_agent_runtime_endpoints(agentRuntimeId=runtime_id)
        for ep in endpoints.get("agentRuntimeEndpoints", []):
            ep_id = ep["agentRuntimeEndpointId"]
            log.info("Deleting endpoint: %s", ep_id)
            if not dry_run:
                client.delete_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointId=ep_id)
                _wait_with_spinner(f"Waiting for endpoint {ep_id} deletion", 15)
    except Exception as e:
        log.warning("Could not list/delete endpoints (non-fatal): %s", e)

    log.info("Deleting runtime: %s", runtime_id)
    if not dry_run:
        try:
            client.delete_agent_runtime(agentRuntimeId=runtime_id)
            console.print("[green]✓ AgentCore Runtime deleted[/green]")
        except Exception as e:
            log.error("Failed to delete runtime: %s", e)
    else:
        log.info("[dry-run] Would delete runtime: %s", runtime_id)


def _teardown_cdk_stacks(phase_name, stacks, config, cfn, dry_run, force):
    console.print(f"\n[bold]━━━ CDK {phase_name} Stacks ━━━[/bold]")
    if not force and not Confirm.ask(f"Destroy {phase_name} stacks: {', '.join(stacks)}?"):
        return
    for stack in stacks:
        if stack_exists(cfn, stack):
            log.info("Destroying %s...", stack)
            run_cdk(["destroy", stack, "--force"], config, dry_run=dry_run)
        else:
            log.info("%s not found — skipping", stack)
    console.print(f"[green]✓ {phase_name} stacks destroyed[/green]")


def _teardown_s3_bucket(session, bucket_name, region, dry_run, force):
    console.print("\n[bold]━━━ Step 4: S3 Workspace Bucket ━━━[/bold]")
    s3 = session.client("s3")
    try:
        s3.head_bucket(Bucket=bucket_name)
    except Exception:
        log.info("Bucket %s not found — skipping", bucket_name)
        return

    if not force and not Confirm.ask(
        f"[red]Empty and delete S3 bucket: {bucket_name}? ALL USER WORKSPACES WILL BE LOST.[/red]"
    ):
        return

    if dry_run:
        log.info("[dry-run] Would empty and delete bucket: %s", bucket_name)
        return

    s3r = session.resource("s3")
    bucket = s3r.Bucket(bucket_name)
    log.info("Deleting all object versions in %s...", bucket_name)
    try:
        bucket.object_versions.delete()
    except Exception as e:
        log.warning("Version deletion error (non-fatal): %s", e)

    log.info("Deleting bucket %s...", bucket_name)
    try:
        bucket.delete()
        console.print("[green]✓ S3 bucket deleted[/green]")
    except Exception as e:
        log.error("Failed to delete bucket: %s", e)


def _teardown_ecr(session, region, dry_run, force):
    console.print("\n[bold]━━━ Step 5: ECR Repository ━━━[/bold]")
    ecr = session.client("ecr")
    try:
        ecr.describe_repositories(repositoryNames=["openclaw-runtime"])
    except ecr.exceptions.RepositoryNotFoundException:
        log.info("ECR repository not found — skipping")
        return

    if not force and not Confirm.ask("Delete ECR repository: openclaw-runtime?"):
        return

    if dry_run:
        log.info("[dry-run] Would delete ECR repository: openclaw-runtime")
        return

    try:
        ecr.delete_repository(repositoryName="openclaw-runtime", force=True)
        console.print("[green]✓ ECR repository deleted[/green]")
    except Exception as e:
        log.error("Failed to delete ECR repository: %s", e)


def _teardown_kms(session, region, dry_run, force):
    console.print("\n[bold]━━━ Step 6: KMS Key ━━━[/bold]")
    kms = session.client("kms")
    try:
        resp = kms.describe_key(KeyId="alias/openclaw/master")
        key_id = resp["KeyMetadata"]["KeyId"]
        key_state = resp["KeyMetadata"]["KeyState"]
    except Exception:
        log.info("KMS key not found — skipping")
        return

    if key_state == "PendingDeletion":
        log.info("KMS key already scheduled for deletion")
        return

    if not force and not Confirm.ask(f"Schedule KMS key for deletion (7-day waiting period): {key_id}?"):
        return

    if dry_run:
        log.info("[dry-run] Would schedule KMS key for deletion: %s", key_id)
        return

    try:
        kms.schedule_key_deletion(KeyId=key_id, PendingWindowInDays=7)
        console.print("[green]✓ KMS key scheduled for deletion in 7 days[/green]")
        console.print(f"  To cancel: [bold]aws kms cancel-key-deletion --key-id {key_id} --region {region}[/bold]")
    except Exception as e:
        log.error("Failed to schedule KMS key deletion: %s", e)


def _teardown_log_groups(session, region, dry_run, force):
    console.print("\n[bold]━━━ Step 7: CloudWatch Log Groups ━━━[/bold]")
    logs = session.client("logs")
    paginator = logs.get_paginator("describe_log_groups")
    groups = []
    for page in paginator.paginate(logGroupNamePrefix="/aws/lambda/openclaw"):
        groups.extend(page.get("logGroups", []))

    if not groups:
        log.info("No log groups found — skipping")
        return

    names = [g["logGroupName"] for g in groups]
    if not force and not Confirm.ask(f"Delete {len(names)} CloudWatch log groups?"):
        return

    for name in names:
        log.info("Deleting log group: %s", name)
        if not dry_run:
            try:
                logs.delete_log_group(logGroupName=name)
            except Exception as e:
                log.warning("Failed to delete %s: %s", name, e)
    console.print("[green]✓ Log groups deleted[/green]")


def _teardown_telegram_webhook(session, region, dry_run, force):
    console.print("\n[bold]━━━ Step 8: Telegram Webhook (optional) ━━━[/bold]")
    sm = session.client("secretsmanager")
    try:
        resp = sm.get_secret_value(SecretId="openclaw/channels/telegram")
        token = resp["SecretString"]
    except Exception:
        log.info("No Telegram token found — skipping")
        return

    if not force and not Confirm.ask("Deregister Telegram webhook?"):
        return

    if dry_run:
        log.info("[dry-run] Would deregister Telegram webhook")
        return

    try:
        url = f"https://api.telegram.org/bot{token}/deleteWebhook"
        urllib.request.urlopen(url, timeout=10)  # nosec B310
        console.print("[green]✓ Telegram webhook deregistered[/green]")
    except Exception as e:
        log.warning("Failed to deregister Telegram webhook: %s", e)


# ── Channel Setup ─────────────────────────────────────────────────────────


def cmd_setup(args: argparse.Namespace) -> None:
    config = load_config()
    session = get_boto_session(config)
    verify_credentials(session)
    region = config["region"]
    prefix = config.get("stack_prefix", "OpenClaw")

    cfn = session.client("cloudformation")
    api_url = get_stack_output(cfn, f"{prefix}Router", "ApiUrl")
    if not api_url:
        console.print(f"[red]✗ {prefix}Router stack not deployed yet. Run Phase 3 first.[/red]")
        sys.exit(1)

    channel = args.channel
    sm = session.client("secretsmanager")

    if channel == "telegram":
        _setup_telegram(session, sm, cfn, api_url, region, prefix)
    elif channel == "slack":
        _setup_slack(sm, api_url, region)
    elif channel == "whatsapp":
        _setup_whatsapp(sm, api_url, region)
    elif channel == "discord":
        _setup_discord(sm, api_url, region)


def _setup_telegram(session, sm, cfn, api_url, region, prefix):
    console.print(Panel("[bold]OpenClaw Telegram Setup[/bold]", border_style="blue"))

    # Check for existing token
    token = ""
    try:
        resp = sm.get_secret_value(SecretId="openclaw/channels/telegram")
        token = resp["SecretString"]
        log.info("Found existing Telegram token in Secrets Manager")
    except sm.exceptions.ResourceNotFoundException:
        pass

    import re

    _tg_token_re = re.compile(r"^\d+:[\w-]{35,}$")
    if not token or token == "null" or not _tg_token_re.match(token):
        if token and token not in ("null", ""):
            console.print("\n[yellow]Existing secret doesn't look like a valid Telegram token.[/yellow]")
        else:
            console.print("\n[yellow]No Telegram bot token found in Secrets Manager.[/yellow]")
        console.print("  1. Talk to [bold]@BotFather[/bold] on Telegram to create a bot")
        console.print("  2. Copy the bot token (format: 1234567890:ABCdef...)\n")
        token = Prompt.ask("Enter your bot token")
        sm.update_secret(SecretId="openclaw/channels/telegram", SecretString=token)
        console.print("[green]✓ Token stored in Secrets Manager[/green]")

    # Register webhook
    webhook_url = f"{api_url}/webhook/telegram"
    log.info("Registering webhook: %s", webhook_url)
    try:
        url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}"
        with urllib.request.urlopen(url, timeout=10) as r:  # nosec B310
            result = json.loads(r.read())
        if result.get("ok"):
            console.print("[green]✓ Telegram webhook registered[/green]")
        else:
            console.print(f"[red]✗ Telegram API error: {result}[/red]")
    except Exception as e:
        log.error("Failed to register webhook: %s", e)

    # Add first user
    console.print("\nGet your numeric Telegram user ID from [bold]@userinfobot[/bold]")
    console.print("[yellow]Note: your user ID is a number like 123456789, NOT your username.[/yellow]")
    tg_user_id = Prompt.ask("Your Telegram user ID (numeric)")
    display_name = Prompt.ask("Your display name", default="Admin")
    _add_user_to_table(session, cfn, f"{prefix}Router", region, "telegram", tg_user_id, display_name)


def _setup_slack(sm, api_url, region):
    console.print(Panel("[bold]OpenClaw Slack Setup[/bold]", border_style="blue"))
    console.print("\n1. Go to [bold]https://api.slack.com/apps[/bold] and create a new app")
    console.print("2. Under 'OAuth & Permissions', add Bot Token Scopes:")
    console.print("   chat:write, app_mentions:read, im:history, im:read, im:write")
    console.print("3. Install the app to your workspace")
    console.print("4. Copy the Bot User OAuth Token (xoxb-...)")
    console.print("5. Copy the Signing Secret from 'Basic Information'\n")

    bot_token = Prompt.ask("Bot Token (xoxb-...)")
    signing_secret = Prompt.ask("Signing Secret")

    secret_json = json.dumps({"botToken": bot_token, "signingSecret": signing_secret})
    sm.update_secret(SecretId="openclaw/channels/slack", SecretString=secret_json)
    console.print("[green]✓ Slack credentials stored[/green]")

    webhook_url = f"{api_url}/webhook/slack"
    console.print(f"\n6. Under 'Event Subscriptions', set Request URL to:\n   [bold]{webhook_url}[/bold]")
    console.print("7. Subscribe to bot events: message.im, app_mention")
    console.print("\n[green]Done![/green] The bot will respond to DMs and @mentions.")


def _setup_whatsapp(sm, api_url, region):
    console.print(Panel("[bold]OpenClaw WhatsApp Setup[/bold]", border_style="blue"))
    console.print("\n1. Go to [bold]https://developers.facebook.com/apps/[/bold]")
    console.print("2. Create or select your app → WhatsApp → API Setup")
    console.print("3. Copy the Permanent Token, Phone Number ID, and App Secret\n")

    wa_token = Prompt.ask("WhatsApp API Token")
    phone_id = Prompt.ask("Phone Number ID")
    app_secret = Prompt.ask("App Secret (for webhook signature verification)")
    verify_token = Prompt.ask("Webhook Verify Token (choose any string)")

    secret_json = json.dumps(
        {
            "token": wa_token,
            "phoneNumberId": phone_id,
            "appSecret": app_secret,
            "verifyToken": verify_token,
        }
    )
    sm.update_secret(SecretId="openclaw/channels/whatsapp", SecretString=secret_json)
    console.print("[green]✓ WhatsApp credentials stored[/green]")

    webhook_url = f"{api_url}/webhook/whatsapp"
    console.print("\n4. In Meta Developer Console → Webhooks → Configure:")
    console.print(f"   Callback URL: [bold]{webhook_url}[/bold]")
    console.print(f"   Verify Token: [bold]{verify_token}[/bold]")
    console.print("   Subscribe to: messages")
    console.print("\n[green]Done![/green] Send a message to your WhatsApp Business number to test.")


def _setup_discord(sm, api_url, region):
    console.print(Panel("[bold]OpenClaw Discord Setup[/bold]", border_style="blue"))
    console.print("\n1. Go to [bold]https://discord.com/developers/applications[/bold]")
    console.print("2. Create a new application")
    console.print("3. Under 'Bot', create a bot and copy the token")
    console.print("4. Under 'General Information', copy the Application ID and Public Key\n")

    bot_token = Prompt.ask("Bot Token")
    app_id = Prompt.ask("Application ID")
    public_key = Prompt.ask("Public Key")

    secret_json = json.dumps({"botToken": bot_token, "applicationId": app_id, "publicKey": public_key})
    sm.update_secret(SecretId="openclaw/channels/discord", SecretString=secret_json)
    console.print("[green]✓ Discord credentials stored[/green]")

    webhook_url = f"{api_url}/webhook/discord"
    console.print("\n5. Under 'General Information', set Interactions Endpoint URL to:")
    console.print(f"   [bold]{webhook_url}[/bold]")
    console.print("\n6. Under 'OAuth2 → URL Generator':")
    console.print("   Scopes: bot, applications.commands")
    console.print("   Bot Permissions: Send Messages, Read Message History")
    console.print("\n[green]Done![/green] Use slash commands or DM the bot to test.")


# ── Google OAuth Setup ────────────────────────────────────────────────────


def _google_scope_options() -> dict:
    return {
        "1": {
            "label": "Read-only (safe default) — read Gmail, Calendar, Drive; cannot send or modify",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/documents.readonly",
                "https://www.googleapis.com/auth/contacts.readonly",
            ],
        },
        "2": {
            "label": "Full access — read + send email, create/edit calendar events, edit Drive files",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.modify",
                "https://mail.google.com/",
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/documents",
                "https://www.googleapis.com/auth/contacts",
            ],
        },
    }


def _run_oauth_flow(client_id: str, client_secret: str, scopes: list[str], account_hint: str) -> str:
    """Run the local OAuth browser flow and return the refresh token."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError:
        console.print(
            "[red]✗ google-auth-oauthlib is not installed.[/red]\n"
            "  Run: [bold]uv pip install google-auth-oauthlib[/bold]"
        )
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": ["http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    console.print(
        f"\nA browser window will open. Sign in with [bold]{account_hint}[/bold] and grant the requested permissions.\n"
    )
    try:
        flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
        credentials = flow.run_local_server(
            port=0,
            open_browser=True,
            access_type="offline",
            prompt="consent",
        )
        refresh_token = credentials.refresh_token
        if not refresh_token:
            console.print(
                "[red]✗ No refresh token returned.[/red]\n"
                "  This happens when the app was previously authorized.\n"
                "  Go to [bold]https://myaccount.google.com/permissions[/bold],\n"
                "  revoke access for 'OpenClaw', then re-run this wizard."
            )
            sys.exit(1)
        return refresh_token
    except Exception as e:
        console.print(f"[red]✗ OAuth flow failed: {e}[/red]")
        sys.exit(1)


def _load_google_store(sm) -> dict:
    """Load the current multi-account store from Secrets Manager, or return empty."""
    try:
        raw = sm.get_secret_value(SecretId="openclaw/google-oauth")["SecretString"]
        store = json.loads(raw) if raw else {}
        # Back-compat: migrate single-account format
        if store and "accounts" not in store and store.get("refresh_token"):
            email = store.get("account", "")
            store = {
                "accounts": {email: store},
                "default_account": email,
            }
        return store
    except sm.exceptions.ResourceNotFoundException:
        return {}
    except json.JSONDecodeError:
        return {}


def _save_google_store(sm, store: dict) -> None:
    """Write the multi-account store back to Secrets Manager."""
    payload = json.dumps(store, indent=2)
    try:
        sm.update_secret(SecretId="openclaw/google-oauth", SecretString=payload)
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(
            Name="openclaw/google-oauth",
            Description="Google OAuth 2.0 credentials for Gmail/Calendar/Drive/Sheets/Docs",
            SecretString=payload,
        )


def cmd_setup_google(args: argparse.Namespace) -> None:
    """Interactive wizard to add or update a Google account for Gmail/Calendar/Drive.

    Supports multiple accounts. Run once per account — existing accounts are
    preserved. Use --set-default to change which account OpenClaw uses when
    you don't specify one explicitly.

    Usage:
        just setup-google                  # add / update an account
        just setup-google --list           # list configured accounts
        just setup-google --remove EMAIL   # remove an account
        just setup-google --set-default EMAIL
    """
    config = load_config()
    session = get_boto_session(config)
    verify_credentials(session)
    sm = session.client("secretsmanager")

    store = _load_google_store(sm)
    accounts: dict = store.get("accounts", {})

    # ── --list ────────────────────────────────────────────────────────────
    if getattr(args, "list_accounts", False):
        if not accounts:
            console.print("[yellow]No Google accounts configured yet.[/yellow]")
            console.print("Run [bold]just setup-google[/bold] to add one.")
            return
        default = store.get("default_account", "")
        t = Table(title="Configured Google Accounts", border_style="blue")
        t.add_column("Account", style="cyan")
        t.add_column("Label")
        t.add_column("Scope")
        t.add_column("Default")
        for email, creds in accounts.items():
            t.add_row(
                email,
                creds.get("label", ""),
                creds.get("scope_level", "unknown"),
                "✓" if email == default else "",
            )
        console.print(t)
        return

    # ── --remove ──────────────────────────────────────────────────────────
    remove_email = getattr(args, "remove", None)
    if remove_email:
        if remove_email not in accounts:
            console.print(f"[red]✗ Account not found: {remove_email}[/red]")
            sys.exit(1)
        del accounts[remove_email]
        if store.get("default_account") == remove_email:
            store["default_account"] = next(iter(accounts), "")
        store["accounts"] = accounts
        _save_google_store(sm, store)
        console.print(f"[green]✓ Removed {remove_email}[/green]")
        if accounts:
            console.print(f"  New default: {store['default_account']}")
            console.print("  Run [bold]just deploy-phase2[/bold] to apply.")
        return

    # ── --set-default ─────────────────────────────────────────────────────
    set_default = getattr(args, "set_default", None)
    if set_default:
        if set_default not in accounts:
            console.print(f"[red]✗ Account not found: {set_default}[/red]")
            console.print(f"  Configured: {', '.join(accounts.keys()) or 'none'}")
            sys.exit(1)
        store["default_account"] = set_default
        store["accounts"] = accounts
        _save_google_store(sm, store)
        save_config_value("google_account", set_default)
        console.print(f"[green]✓ Default account set to {set_default}[/green]")
        console.print("  Run [bold]just deploy-phase2[/bold] to apply.")
        return

    # ── Add / update an account ───────────────────────────────────────────
    console.print(
        Panel(
            "[bold]OpenClaw — Google Workspace Setup[/bold]\n\n"
            "Connect a Google account so OpenClaw can access Gmail, Calendar,\n"
            "Drive, Sheets, Docs, and Contacts.\n\n"
            + (f"[dim]Currently configured: {', '.join(accounts.keys())}[/dim]\n\n" if accounts else "")
            + "[yellow]You will need a web browser on this machine to complete\n"
            "the one-time OAuth authorization flow.[/yellow]",
            border_style="blue",
        )
    )

    # ── Step 1: Google Cloud Console ──────────────────────────────────────
    console.print("\n[bold cyan]━━━ Step 1: Google Cloud Project ━━━[/bold cyan]")

    # If accounts already exist, offer to reuse the same OAuth client
    reuse_client = False
    existing_client_id = ""
    existing_client_secret = ""
    if accounts:
        first = next(iter(accounts.values()))
        existing_client_id = first.get("client_id", "")
        console.print(
            f"\nYou already have an OAuth client configured (client_id: [dim]{existing_client_id[:30]}...[/dim])."
        )
        reuse = Prompt.ask(
            "Reuse the same OAuth client for this account?",
            choices=["yes", "no"],
            default="yes",
        )
        reuse_client = reuse == "yes"
        if reuse_client:
            existing_client_secret = first.get("client_secret", "")

    if not reuse_client:
        console.print(
            "\n1. Open [bold]https://console.cloud.google.com[/bold]\n"
            "2. Create or select a project\n"
            "3. Enable these APIs under [bold]APIs & Services → Library[/bold]:\n"
            "   • Gmail API\n"
            "   • Google Calendar API\n"
            "   • Google Drive API\n"
            "   • Google Sheets API\n"
            "   • Google Docs API\n"
            "   • People API  (for Contacts)\n"
            "4. Go to [bold]APIs & Services → OAuth consent screen[/bold]\n"
            "   • User Type: External\n"
            "   • App name: OpenClaw\n"
            "   • Add the new account email as a test user\n"
            "5. Go to [bold]APIs & Services → Credentials → Create Credentials → OAuth client ID[/bold]\n"
            "   • Application type: [bold]Desktop app[/bold]\n"
            "   • Name: OpenClaw\n"
        )
        Prompt.ask("Press Enter when you have the Client ID and Client Secret ready")

    client_id = existing_client_id if reuse_client else Prompt.ask("Client ID (ends with .apps.googleusercontent.com)")
    client_secret = existing_client_secret if reuse_client else Prompt.ask("Client Secret")

    google_account = Prompt.ask("Google account email to add")
    label = Prompt.ask("Short label for this account (e.g. personal, work)", default=google_account.split("@")[0])

    # ── Step 2: Choose scopes ─────────────────────────────────────────────
    console.print("\n[bold cyan]━━━ Step 2: Access Scopes ━━━[/bold cyan]")
    console.print(
        "\nChoose the level of access OpenClaw will have for this account.\n"
        "You can use different scope levels per account.\n"
    )
    scope_opts = _google_scope_options()
    for key, opt in scope_opts.items():
        console.print(f"  [bold]{key}[/bold]. {opt['label']}")
    scope_choice = Prompt.ask("Choose scope level", choices=["1", "2"], default="1")
    chosen_scopes = scope_opts[scope_choice]["scopes"]
    scope_level = "readonly" if scope_choice == "1" else "full"
    console.print(f"[green]✓ Using {scope_level} access for {google_account}[/green]")

    # ── Step 3: OAuth authorization flow ─────────────────────────────────
    console.print("\n[bold cyan]━━━ Step 3: Authorize OpenClaw ━━━[/bold cyan]")
    refresh_token = _run_oauth_flow(client_id, client_secret, chosen_scopes, google_account)
    console.print("[green]✓ Authorization successful[/green]")

    # ── Step 4: Store in Secrets Manager ─────────────────────────────────
    console.print("\n[bold cyan]━━━ Step 4: Storing Credentials ━━━[/bold cyan]")

    accounts[google_account] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "scopes": chosen_scopes,
        "scope_level": scope_level,
        "label": label,
    }

    # First account becomes the default; subsequent ones don't change it
    # unless the user explicitly asks
    is_first = len(accounts) == 1
    if is_first:
        store["default_account"] = google_account
    elif not store.get("default_account"):
        store["default_account"] = google_account

    store["accounts"] = accounts
    _save_google_store(sm, store)
    console.print("[green]✓ Credentials stored in Secrets Manager (openclaw/google-oauth)[/green]")

    # ── Step 5: Update cdk.json default ──────────────────────────────────
    save_config_value("google_account", store["default_account"])

    # ── Ask about setting as default if not the first ─────────────────────
    if not is_first and store.get("default_account") != google_account:
        make_default = Prompt.ask(
            f"Set {google_account} as the default account?",
            choices=["yes", "no"],
            default="no",
        )
        if make_default == "yes":
            store["default_account"] = google_account
            _save_google_store(sm, store)
            save_config_value("google_account", google_account)

    # ── Done ──────────────────────────────────────────────────────────────
    all_emails = list(accounts.keys())
    console.print(
        Panel(
            f"[bold green]✓ {google_account} connected![/bold green]\n\n"
            f"Label:    {label}\n"
            f"Access:   {scope_level}\n"
            f"Default:  {store['default_account']}\n"
            f"All accounts ({len(all_emails)}): {', '.join(all_emails)}\n\n"
            "Next steps:\n"
            "  1. Run [bold]just deploy-phase2[/bold] to inject credentials into the container\n"
            "  2. Add more accounts with [bold]just setup-google[/bold]\n"
            "  3. Then message OpenClaw:\n"
            '     [italic]"Check my work email for unread messages"[/italic]\n'
            '     [italic]"Find all invoices in my personal Gmail this month"[/italic]\n'
            '     [italic]"What\'s on my work calendar this week?"[/italic]',
            border_style="green",
        )
    )


def cmd_refresh_google_token(args: argparse.Namespace) -> None:
    """Re-mint the OAuth refresh token for an already-configured Google account.

    Reuses the account's stored OAuth client and scopes, runs the browser
    consent flow once, writes the fresh refresh token back to Secrets Manager,
    and (unless --no-deploy) re-injects it into the AgentCore runtime.

    This is the fix for expired/revoked refresh tokens — common when the Google
    OAuth consent app is still in "Testing" mode, where tokens expire after 7
    days. Publish the consent app for long-lived tokens.

    Usage:
        just refresh-google-token                 # default account
        just refresh-google-token you@gmail.com   # a specific account
    """
    config = load_config()
    session = get_boto_session(config)
    verify_credentials(session)
    sm = session.client("secretsmanager")

    store = _load_google_store(sm)
    accounts: dict = store.get("accounts", {})
    if not accounts:
        console.print("[red]✗ No Google accounts configured.[/red] Run [bold]just setup-google[/bold] first.")
        sys.exit(1)

    email = getattr(args, "email", None) or store.get("default_account") or next(iter(accounts))
    if email not in accounts:
        console.print(f"[red]✗ Account not found: {email}[/red]")
        console.print(f"  Configured: {', '.join(accounts.keys())}")
        sys.exit(1)

    creds = accounts[email]
    client_id = creds.get("client_id", "")
    client_secret = creds.get("client_secret", "")
    scopes = creds.get("scopes", [])
    if not client_id or not client_secret or not scopes:
        console.print(f"[red]✗ Stored client/scopes incomplete for {email}.[/red] Re-run [bold]just setup-google[/bold].")
        sys.exit(1)

    console.print(
        Panel(
            f"[bold]Re-authorize {email}[/bold]\n\n"
            "A browser window will open. Sign in with this account and grant the\n"
            "requested permissions to mint a fresh refresh token.\n\n"
            "[yellow]Tip: if tokens keep expiring after ~7 days, publish your\n"
            "Google OAuth consent app (move it out of Testing).[/yellow]",
            border_style="blue",
        )
    )

    # Reuse the existing OAuth flow helper — same scopes, same client.
    refresh_token = _run_oauth_flow(client_id, client_secret, scopes, email)
    console.print("[green]✓ New refresh token obtained[/green]")

    creds["refresh_token"] = refresh_token
    accounts[email] = creds
    store["accounts"] = accounts
    _save_google_store(sm, store)
    console.print("[green]✓ Updated openclaw/google-oauth in Secrets Manager[/green]")

    if getattr(args, "no_deploy", False):
        console.print("\nSkipped redeploy (--no-deploy). Run [bold]just deploy-phase2[/bold] to apply.")
        return

    console.print("\n[cyan]Re-injecting credentials into the AgentCore runtime…[/cyan]")
    # _deploy_phase2 reads the secret and updates the runtime env vars.
    deploy_args = argparse.Namespace(phase="2", local=False)
    cmd_deploy(deploy_args)


# ── User Management ───────────────────────────────────────────────────────

CHANNEL_PREFIX_MAP = {"telegram": "tg", "slack": "sl", "whatsapp": "wa", "discord": "dc"}


def _add_user_to_table(session, cfn, router_stack, region, channel, channel_user_id, display_name):
    table_name = get_stack_output(cfn, router_stack, "IdentityTableName")
    if not table_name:
        console.print(f"[red]✗ Could not find identity table from {router_stack}[/red]")
        sys.exit(1)

    prefix = CHANNEL_PREFIX_MAP.get(channel, channel[:2])
    user_id = f"{prefix}__{channel_user_id}"
    ddb = session.resource("dynamodb")
    table = ddb.Table(table_name)
    table.put_item(
        Item={
            "pk": f"USER#{user_id}",
            "sk": "PROFILE",
            "user_id": user_id,
            "channel_id": f"{channel}:{channel_user_id}",
            "display_name": display_name,
            "created_at": int(time.time()),
            "status": "active",
        }
    )
    console.print(f"[green]✓ Added {channel}:{channel_user_id} ({display_name})[/green]")


def cmd_users(args: argparse.Namespace) -> None:
    config = load_config()
    session = get_boto_session(config)
    verify_credentials(session)
    region = config["region"]
    prefix = config.get("stack_prefix", "OpenClaw")

    cfn = session.client("cloudformation")
    table_name = get_stack_output(cfn, f"{prefix}Router", "IdentityTableName")
    if not table_name:
        console.print(f"[red]✗ {prefix}Router stack not deployed yet.[/red]")
        sys.exit(1)

    ddb = session.resource("dynamodb")
    table = ddb.Table(table_name)

    if args.action == "list":
        resp = table.scan(
            FilterExpression="sk = :sk",
            ExpressionAttributeValues={":sk": "PROFILE"},
        )
        items = resp.get("Items", [])
        t = Table(title="Registered Users", border_style="blue")
        t.add_column("User ID", style="cyan")
        t.add_column("Channel ID")
        t.add_column("Display Name")
        t.add_column("Status")
        t.add_column("Created")
        for item in sorted(items, key=lambda x: x.get("created_at", 0)):
            created = time.strftime("%Y-%m-%d", time.localtime(int(item.get("created_at", 0))))
            t.add_row(
                item.get("user_id", ""),
                item.get("channel_id", ""),
                item.get("display_name", ""),
                item.get("status", ""),
                created,
            )
        console.print(t)
        console.print(f"Total: {len(items)} user(s)")

    elif args.action == "add":
        channel_id = args.channel_id
        if ":" not in channel_id:
            console.print("[red]✗ Format must be channel:user_id (e.g. telegram:123456789)[/red]")
            sys.exit(1)
        channel, uid = channel_id.split(":", 1)
        display_name = args.name or "User"
        _add_user_to_table(session, cfn, f"{prefix}Router", region, channel, uid, display_name)

    elif args.action == "remove":
        channel_id = args.channel_id
        if ":" not in channel_id:
            console.print("[red]✗ Format must be channel:user_id[/red]")
            sys.exit(1)
        channel, uid = channel_id.split(":", 1)
        prefix_ch = CHANNEL_PREFIX_MAP.get(channel, channel[:2])
        user_id = f"{prefix_ch}__{uid}"
        table.delete_item(Key={"pk": f"USER#{user_id}", "sk": "PROFILE"})
        console.print(f"[green]✓ Removed {channel_id}[/green]")


# ── Outputs ───────────────────────────────────────────────────────────────


def cmd_outputs(args: argparse.Namespace) -> None:
    config = load_config()
    session = get_boto_session(config)
    verify_credentials(session)
    prefix = config.get("stack_prefix", "OpenClaw")
    cfn = session.client("cloudformation")

    stacks = ["Vpc", "Security", "Guardrails", "Observability", "AgentCore", "Router", "Cron", "TokenMonitoring"]

    for stack_suffix in stacks:
        stack_name = f"{prefix}{stack_suffix}"
        console.print(f"\n[bold cyan]━━━ {stack_name} ━━━[/bold cyan]")
        try:
            resp = cfn.describe_stacks(StackName=stack_name)
            outputs = resp["Stacks"][0].get("Outputs", [])
            if outputs:
                t = Table(show_header=True, border_style="dim")
                t.add_column("Key", style="cyan")
                t.add_column("Value")
                t.add_column("Description")
                for o in outputs:
                    t.add_row(
                        o.get("OutputKey", ""),
                        o.get("OutputValue", ""),
                        o.get("Description", ""),
                    )
                console.print(t)
            else:
                console.print("  (no outputs)")
        except Exception:
            console.print("  [dim](not deployed)[/dim]")


# ── Status ────────────────────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> None:
    config = load_config()
    session = get_boto_session(config)
    verify_credentials(session)
    region = config["region"]
    prefix = config.get("stack_prefix", "OpenClaw")
    cfn = session.client("cloudformation")

    t = Table(title="OpenClaw Stack Status", border_style="blue")
    t.add_column("Stack")
    t.add_column("Status")
    t.add_column("Last Updated")

    stacks = ["Vpc", "Security", "Guardrails", "Observability", "AgentCore", "Router", "Cron", "TokenMonitoring"]

    for suffix in stacks:
        name = f"{prefix}{suffix}"
        try:
            resp = cfn.describe_stacks(StackName=name)
            stack = resp["Stacks"][0]
            status = stack["StackStatus"]
            updated = stack.get("LastUpdatedTime", stack.get("CreationTime", ""))
            if hasattr(updated, "strftime"):
                updated = updated.strftime("%Y-%m-%d %H:%M")
            color = "green" if "COMPLETE" in status else "red" if "FAILED" in status else "yellow"
            t.add_row(name, f"[{color}]{status}[/{color}]", str(updated))
        except Exception:
            t.add_row(name, "[dim]NOT DEPLOYED[/dim]", "")

    console.print(t)

    # AgentCore Runtime status
    runtime_id = config.get("runtime_id", "")
    if runtime_id and runtime_id not in ("", "null"):
        console.print(f"\n[bold]AgentCore Runtime:[/bold] {runtime_id}")
        try:
            ac = session.client("bedrock-agentcore", region_name=region)
            resp = ac.get_agent_runtime(agentRuntimeId=runtime_id)
            status = resp.get("status", "unknown")
            color = "green" if status == "ACTIVE" else "yellow"
            console.print(f"  Status: [{color}]{status}[/{color}]")
        except Exception as e:
            console.print(f"  [dim]Could not fetch runtime status: {e}[/dim]")


# ── Logs ──────────────────────────────────────────────────────────────────


def cmd_logs(args: argparse.Namespace) -> None:
    config = load_config()
    session = get_boto_session(config)
    verify_credentials(session)
    region = config["region"]
    profile = config.get("aws_profile", "")

    fn_map = {"router": "openclaw-router", "cron": "openclaw-cron"}
    fn_name = fn_map.get(args.function)
    if not fn_name:
        console.print(f"[red]Unknown function: {args.function}. Use: router, cron[/red]")
        sys.exit(1)

    log_group = f"/aws/lambda/{fn_name}"
    log.info("Tailing log group: %s", log_group)

    # Delegate to aws logs tail (handles --follow natively)
    cmd = ["aws", "logs", "tail", log_group, "--region", region]
    if args.follow:
        cmd.append("--follow")
    env = os.environ.copy()
    env["AWS_DEFAULT_REGION"] = region
    if profile and profile not in ("None", "REPLACE_WITH_YOUR_SSO_PROFILE_NAME"):
        env["AWS_PROFILE"] = profile
    subprocess.run(cmd, env=env)


def cmd_gog_logs(args: argparse.Namespace) -> None:
    """Tail the AgentCore runtime logs filtered for gog / Google Workspace init.

    Surfaces the entrypoint's gog initialization and the `gog auth doctor`
    health check so you can confirm credentials loaded after a deploy.

    Usage:
        just gog-logs            # recent gog/entrypoint lines, follow
        just gog-logs --since 1h
    """
    config = load_config()
    session = get_boto_session(config)
    verify_credentials(session)
    region = config["region"]
    profile = config.get("aws_profile", "")

    runtime_id = config.get("runtime_id", "")
    if not runtime_id or runtime_id in ("", "null", "REPLACE_WITH_RUNTIME_ID"):
        console.print(
            "[red]✗ No runtime_id in config.[/red] Deploy the runtime first "
            "([bold]just deploy-phase2[/bold])."
        )
        sys.exit(1)

    log_group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    log.info("Tailing %s (filtered for gog/entrypoint)", log_group)

    # CloudWatch Logs filter pattern: match the entrypoint's gog markers and
    # the gog binary's own output. Quoted terms are matched as substrings.
    filter_pattern = '?gog ?gog_init ?GOG ?"Google Workspace" ?"auth doctor" ?keyring'

    cmd = [
        "aws", "logs", "tail", log_group,
        "--region", region,
        "--since", getattr(args, "since", None) or "30m",
        "--format", "short",
        "--filter-pattern", filter_pattern,
    ]
    if getattr(args, "follow", True):
        cmd.append("--follow")

    env = os.environ.copy()
    env["AWS_DEFAULT_REGION"] = region
    if profile and profile not in ("None", "REPLACE_WITH_YOUR_SSO_PROFILE_NAME"):
        env["AWS_PROFILE"] = profile
    subprocess.run(cmd, env=env)


# ── Utilities ─────────────────────────────────────────────────────────────


def _run_subprocess(cmd: list[str], input_text: str = None) -> None:
    """Run a subprocess, raising on failure."""
    log.info("Running: %s", " ".join(cmd[:3]) + (" ..." if len(cmd) > 3 else ""))
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True if input_text else None,
        capture_output=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")


def _find_executable(name: str) -> Optional[str]:
    import shutil

    return shutil.which(name)


def _wait_with_spinner(message: str, seconds: int) -> None:
    with Progress(SpinnerColumn(), TextColumn(message), transient=True) as p:
        p.add_task("", total=None)
        time.sleep(seconds)


# ── Argument parser ───────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="OpenClaw AgentCore Operations CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # deploy
    p_deploy = sub.add_parser("deploy", help="Deploy OpenClaw infrastructure")
    p_deploy.add_argument("--phase", choices=["1", "2", "3"], help="Deploy a specific phase only")
    p_deploy.add_argument(
        "--local",
        action="store_true",
        help="Phase 2: build image from local openclaw/ directory instead of pulling from Docker Hub",
    )

    # build-image — build and push the openclaw image without full Phase 2 deploy
    p_build = sub.add_parser("build-image", help="Build openclaw image from local source and push to ECR")
    p_build.add_argument(
        "--source",
        choices=["local", "dockerhub"],
        default="local",
        help="Image source: local=build from openclaw/ dir, dockerhub=pull ffactory/openclaw:latest",
    )

    # teardown
    p_tear = sub.add_parser("teardown", help="Remove all OpenClaw AWS resources")
    p_tear.add_argument("--force", action="store_true", help="Skip confirmation prompts")
    p_tear.add_argument("--dry-run", action="store_true", help="Print what would be deleted")

    # setup
    p_setup = sub.add_parser("setup", help="Configure a messaging channel")
    p_setup.add_argument("channel", choices=["telegram", "slack", "whatsapp", "discord"])

    # setup-google
    p_google = sub.add_parser(
        "setup-google",
        help="Connect Google Workspace (Gmail, Calendar, Drive, Sheets, Docs)",
    )
    p_google.add_argument(
        "--list",
        dest="list_accounts",
        action="store_true",
        help="List all configured Google accounts",
    )
    p_google.add_argument(
        "--remove",
        metavar="EMAIL",
        help="Remove a Google account",
    )
    p_google.add_argument(
        "--set-default",
        metavar="EMAIL",
        help="Set the default Google account",
    )

    # refresh-google-token — re-mint an expired/revoked refresh token
    p_refresh = sub.add_parser(
        "refresh-google-token",
        help="Re-authorize a configured Google account and store a fresh refresh token",
    )
    p_refresh.add_argument(
        "email",
        nargs="?",
        help="Account email to re-authorize (default: the configured default account)",
    )
    p_refresh.add_argument(
        "--no-deploy",
        action="store_true",
        help="Update the secret only; do not re-inject into the runtime",
    )

    # users
    p_users = sub.add_parser("users", help="Manage the user allowlist")
    users_sub = p_users.add_subparsers(dest="action", required=True)
    users_sub.add_parser("list", help="List all registered users")
    p_add = users_sub.add_parser("add", help="Add a user (e.g. telegram:123456789)")
    p_add.add_argument("channel_id", help="channel:user_id")
    p_add.add_argument("--name", help="Display name")
    p_remove = users_sub.add_parser("remove", help="Remove a user")
    p_remove.add_argument("channel_id", help="channel:user_id")

    # outputs
    sub.add_parser("outputs", help="Show all CloudFormation stack outputs")

    # status
    sub.add_parser("status", help="Show deployment status of all stacks")

    # logs
    p_logs = sub.add_parser("logs", help="Tail Lambda logs")
    p_logs.add_argument("function", choices=["router", "cron"])
    p_logs.add_argument("--follow", "-f", action="store_true", help="Follow log output")

    # gog-logs — tail AgentCore runtime logs filtered for gog/Google init
    p_goglogs = sub.add_parser(
        "gog-logs",
        help="Tail AgentCore runtime logs filtered for gog / Google Workspace init",
    )
    p_goglogs.add_argument(
        "--since",
        default="30m",
        help="How far back to start (e.g. 10m, 1h, 2d). Default: 30m",
    )
    p_goglogs.add_argument(
        "--no-follow",
        dest="follow",
        action="store_false",
        help="Print matching lines and exit instead of following",
    )

    return parser


def cmd_build_image(args: argparse.Namespace) -> None:
    """Build the openclaw image and push to ECR without running a full Phase 2 deploy."""
    config = load_config()
    session = get_boto_session(config)
    account = verify_credentials(session)
    region = config["region"]
    use_local = args.source == "local"

    ecr_repo = f"{account}.dkr.ecr.{region}.amazonaws.com/openclaw-runtime"

    console.print(
        Panel(
            f"[bold]OpenClaw Image Build[/bold]\n"
            f"Source:  {'local openclaw/ directory' if use_local else config['docker_image']}\n"
            f"Target:  {ecr_repo}:latest",
            border_style="blue",
        )
    )

    # Ensure ECR repo exists
    ecr = session.client("ecr")
    try:
        ecr.describe_repositories(repositoryNames=["openclaw-runtime"])
    except ecr.exceptions.RepositoryNotFoundException:
        log.info("Creating ECR repository: openclaw-runtime")
        ecr.create_repository(
            repositoryName="openclaw-runtime",
            imageScanningConfiguration={"scanOnPush": True},
        )

    # Docker login
    token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    import base64

    username, password = base64.b64decode(token).decode().split(":", 1)
    _run_subprocess(["docker", "login", "--username", username, "--password-stdin", ecr_repo], input_text=password)

    if use_local:
        openclaw_dir = PROJECT_ROOT.parent / "openclaw"
        if not openclaw_dir.exists():
            console.print(f"[red]✗ openclaw/ directory not found at {openclaw_dir}[/red]")
            sys.exit(1)
        log.info("Building from %s (linux/arm64)...", openclaw_dir)
        _run_subprocess(
            [
                "docker",
                "build",
                "--platform",
                "linux/arm64",
                "-t",
                f"{ecr_repo}:latest",
                str(openclaw_dir),
            ]
        )
        console.print("[green]✓ Image built from local source[/green]")
    else:
        docker_image = config["docker_image"]
        log.info("Pulling %s (linux/arm64)...", docker_image)
        _run_subprocess(["docker", "pull", "--platform", "linux/arm64", docker_image])
        _run_subprocess(["docker", "tag", docker_image, f"{ecr_repo}:latest"])

    log.info("Pushing to ECR...")
    _run_subprocess(["docker", "push", f"{ecr_repo}:latest"])
    console.print("[green]✓ Image pushed to ECR[/green]")
    console.print("\nNew sessions will use the updated image automatically.")
    console.print("Existing sessions continue until they idle-terminate (30 min).")


def main():
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "deploy": cmd_deploy,
        "build-image": cmd_build_image,
        "teardown": cmd_teardown,
        "setup": cmd_setup,
        "setup-google": cmd_setup_google,
        "refresh-google-token": cmd_refresh_google_token,
        "users": cmd_users,
        "outputs": cmd_outputs,
        "status": cmd_status,
        "logs": cmd_logs,
        "gog-logs": cmd_gog_logs,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
