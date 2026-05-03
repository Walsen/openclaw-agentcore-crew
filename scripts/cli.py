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
    """Create a boto3 session using SSO profile from cdk.json."""
    profile = config.get("aws_profile", "")
    region = config.get("region", "us-east-1")
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
    profile = config.get("aws_profile", "")
    region = config.get("region", "us-east-1")
    env = os.environ.copy()
    env["AWS_DEFAULT_REGION"] = region
    env["JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION"] = "1"
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

    console.print(Panel(
        f"[bold]OpenClaw AgentCore Deployment[/bold]\n"
        f"Account: {account}\n"
        f"Region:  {region}\n"
        f"Image:   {config['docker_image']}\n"
        f"Profile: {config.get('aws_profile', '(default)')}",
        border_style="blue",
    ))

    if phase in (None, "1"):
        _deploy_phase1(config, prefix)
    if phase in (None, "2"):
        _deploy_phase2(config, session, account, region, prefix)
    if phase in (None, "3"):
        _deploy_phase3(config, prefix)


def _deploy_phase1(config: dict, prefix: str) -> None:
    console.print("\n[bold cyan]━━━ Phase 1: Foundation Stacks ━━━[/bold cyan]")
    stacks = [f"{prefix}Vpc", f"{prefix}Security", f"{prefix}Guardrails", f"{prefix}Observability"]
    run_cdk(["deploy"] + stacks + ["--require-approval", "never"], config)
    console.print("[green]✓ Phase 1 complete[/green]")


def _deploy_phase2(config: dict, session: boto3.Session, account: str, region: str, prefix: str) -> None:
    console.print("\n[bold cyan]━━━ Phase 2: AgentCore Runtime ━━━[/bold cyan]")
    cfn = session.client("cloudformation")

    # Ensure AgentCore stack is deployed first (needed for role ARN + SG)
    agentcore_stack = f"{prefix}AgentCore"
    if not stack_exists(cfn, agentcore_stack):
        log.info("%s not yet deployed — deploying it first", agentcore_stack)
        run_cdk(["deploy", agentcore_stack, "--require-approval", "never"], config)

    role_arn = get_stack_output(cfn, agentcore_stack, "ExecutionRoleArn")
    sg_id = get_stack_output(cfn, agentcore_stack, "SecurityGroupId")
    subnets_raw = get_stack_output(cfn, agentcore_stack, "PrivateSubnetIds")

    if not role_arn or not sg_id or not subnets_raw:
        console.print(f"[red]✗ Could not read outputs from {agentcore_stack}[/red]")
        sys.exit(1)

    subnets = [s.strip() for s in subnets_raw.split(",")]
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

    # Docker login, pull, tag, push
    log.info("Authenticating Docker with ECR...")
    token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    import base64
    username, password = base64.b64decode(token).decode().split(":", 1)
    _run_subprocess(["docker", "login", "--username", username, "--password-stdin",
                     ecr_repo], input_text=password)

    log.info("Pulling %s (linux/arm64)...", docker_image)
    _run_subprocess(["docker", "pull", "--platform", "linux/arm64", docker_image])

    log.info("Tagging and pushing to ECR...")
    _run_subprocess(["docker", "tag", docker_image, f"{ecr_repo}:latest"])
    _run_subprocess(["docker", "push", f"{ecr_repo}:latest"])
    console.print("[green]✓ Image pushed to ECR[/green]")

    # Write .bedrock_agentcore.yaml
    yaml_path = PROJECT_ROOT / ".bedrock_agentcore.yaml"
    subnets_yaml = ", ".join(subnets)
    yaml_content = f"""agent_runtime:
  name: openclaw-agent
  image: {ecr_repo}:latest
  platform: linux/arm64
  role_arn: {role_arn}
  network:
    mode: VPC
    security_groups:
      - {sg_id}
    subnets: [{subnets_yaml}]
  lifecycle:
    idle_timeout: {config['session_idle_timeout']}
    max_lifetime: {config['session_max_lifetime']}
  environment:
    S3_BUCKET: {config['stack_prefix'].lower()}-workspaces-{account}-{region}
    STACK_NAME: {config['stack_prefix']}
    AWS_REGION: {region}
    BEDROCK_MODEL_ID: {config['default_model_id']}
"""
    yaml_path.write_text(yaml_content)
    log.info("Written: %s", yaml_path)

    # Deploy via agentcore CLI
    agentcore_bin = _find_executable("agentcore")
    if not agentcore_bin:
        console.print("[yellow]⚠ agentcore CLI not found.[/yellow]")
        console.print("  Install with: [bold]uv tool install bedrock-agentcore-toolkit[/bold]")
        console.print(f"  Then run: [bold]agentcore deploy --config {yaml_path}[/bold]")
        console.print("\n  After deployment, add the runtime_id to cdk.json:")
        console.print('  [bold]"runtime_id": "openclaw_agent-XXXXXXXXXX"[/bold]')
        return

    log.info("Deploying AgentCore Runtime...")
    result = subprocess.run(
        [agentcore_bin, "deploy", "--config", str(yaml_path)],
        capture_output=False,
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        console.print("[red]✗ agentcore deploy failed[/red]")
        sys.exit(1)

    console.print("[green]✓ Phase 2 complete[/green]")
    console.print("\n[yellow]⚠ Add the runtime_id to cdk.json before deploying Phase 3:[/yellow]")
    console.print('  [bold]"runtime_id": "openclaw_agent-XXXXXXXXXX"[/bold]')


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
        console.print(Panel(
            f"[bold green]Deployment Complete![/bold green]\n\n"
            f"API URL: [bold]{api_url}[/bold]\n\n"
            "Next steps:\n"
            "  1. Run: [bold]just setup-telegram[/bold] (and other channels)\n"
            "  2. Run: [bold]just add-user telegram:YOUR_ID 'Your Name'[/bold]",
            border_style="green",
        ))


# ── Teardown ──────────────────────────────────────────────────────────────

def cmd_teardown(args: argparse.Namespace) -> None:
    config = load_config()
    session = get_boto_session(config)
    account = verify_credentials(session)
    region = config["region"]
    prefix = config.get("stack_prefix", "OpenClaw")
    force = args.force
    dry_run = args.dry_run

    console.print(Panel(
        f"[bold red]OpenClaw — Full Teardown[/bold red]\n"
        + ("[bold yellow]MODE: DRY RUN — nothing will be deleted[/bold yellow]\n" if dry_run else "")
        + f"Region:  {region}\n"
        f"Account: {account}",
        border_style="red",
    ))

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

    console.print(Panel(
        "[bold]Teardown Complete[/bold]\n\n"
        "[yellow]Resources NOT deleted (manual cleanup needed):[/yellow]\n"
        "• Secrets Manager secrets (contain bot tokens)\n"
        f"  aws secretsmanager list-secrets --region {region}\n"
        "• DynamoDB identity table: openclaw-identity\n"
        "• CDK bootstrap stack: CDKToolkit",
        border_style="yellow",
    ))


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
                client.delete_agent_runtime_endpoint(
                    agentRuntimeId=runtime_id, endpointId=ep_id
                )
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

    if not force and not Confirm.ask(
        f"Schedule KMS key for deletion (7-day waiting period): {key_id}?"
    ):
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
        urllib.request.urlopen(url, timeout=10)
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

    if not token or token == "null":
        console.print("\n[yellow]No Telegram bot token found in Secrets Manager.[/yellow]")
        console.print("  1. Talk to [bold]@BotFather[/bold] on Telegram to create a bot")
        console.print("  2. Copy the bot token\n")
        token = Prompt.ask("Enter your bot token")
        sm.update_secret(SecretId="openclaw/channels/telegram", SecretString=token)
        console.print("[green]✓ Token stored in Secrets Manager[/green]")

    # Register webhook
    webhook_url = f"{api_url}/webhook/telegram"
    log.info("Registering webhook: %s", webhook_url)
    try:
        url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}"
        with urllib.request.urlopen(url, timeout=10) as r:
            result = json.loads(r.read())
        if result.get("ok"):
            console.print("[green]✓ Telegram webhook registered[/green]")
        else:
            console.print(f"[red]✗ Telegram API error: {result}[/red]")
    except Exception as e:
        log.error("Failed to register webhook: %s", e)

    # Add first user
    console.print("\nGet your Telegram user ID from [bold]@userinfobot[/bold]")
    tg_user_id = Prompt.ask("Your Telegram user ID")
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

    secret_json = json.dumps({
        "token": wa_token,
        "phoneNumberId": phone_id,
        "appSecret": app_secret,
        "verifyToken": verify_token,
    })
    sm.update_secret(SecretId="openclaw/channels/whatsapp", SecretString=secret_json)
    console.print("[green]✓ WhatsApp credentials stored[/green]")

    webhook_url = f"{api_url}/webhook/whatsapp"
    console.print(f"\n4. In Meta Developer Console → Webhooks → Configure:")
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
    console.print(f"\n5. Under 'General Information', set Interactions Endpoint URL to:")
    console.print(f"   [bold]{webhook_url}[/bold]")
    console.print("\n6. Under 'OAuth2 → URL Generator':")
    console.print("   Scopes: bot, applications.commands")
    console.print("   Bot Permissions: Send Messages, Read Message History")
    console.print("\n[green]Done![/green] Use slash commands or DM the bot to test.")


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
    table.put_item(Item={
        "pk": f"USER#{user_id}",
        "sk": "PROFILE",
        "user_id": user_id,
        "channel_id": f"{channel}:{channel_user_id}",
        "display_name": display_name,
        "created_at": int(time.time()),
        "status": "active",
    })
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
    region = config["region"]
    prefix = config.get("stack_prefix", "OpenClaw")
    cfn = session.client("cloudformation")

    stacks = ["Vpc", "Security", "Guardrails", "Observability",
              "AgentCore", "Router", "Cron", "TokenMonitoring"]

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
    account = verify_credentials(session)
    region = config["region"]
    prefix = config.get("stack_prefix", "OpenClaw")
    cfn = session.client("cloudformation")

    t = Table(title="OpenClaw Stack Status", border_style="blue")
    t.add_column("Stack")
    t.add_column("Status")
    t.add_column("Last Updated")

    stacks = ["Vpc", "Security", "Guardrails", "Observability",
              "AgentCore", "Router", "Cron", "TokenMonitoring"]

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

    # teardown
    p_tear = sub.add_parser("teardown", help="Remove all OpenClaw AWS resources")
    p_tear.add_argument("--force", action="store_true", help="Skip confirmation prompts")
    p_tear.add_argument("--dry-run", action="store_true", help="Print what would be deleted")

    # setup
    p_setup = sub.add_parser("setup", help="Configure a messaging channel")
    p_setup.add_argument("channel", choices=["telegram", "slack", "whatsapp", "discord"])

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

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "deploy":   cmd_deploy,
        "teardown": cmd_teardown,
        "setup":    cmd_setup,
        "users":    cmd_users,
        "outputs":  cmd_outputs,
        "status":   cmd_status,
        "logs":     cmd_logs,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
