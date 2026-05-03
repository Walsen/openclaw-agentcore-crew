#!/usr/bin/env bash
# deploy.sh — 3-phase deployment for OpenClaw on AgentCore
#
# Usage:
#   ./scripts/deploy.sh              # Full deploy (all 3 phases)
#   ./scripts/deploy.sh --phase1     # Foundation stacks only
#   ./scripts/deploy.sh --phase2     # AgentCore Runtime only
#   ./scripts/deploy.sh --phase3     # Application stacks only
#
# Prerequisites:
#   - AWS CLI v2 configured with appropriate credentials
#   - Node.js + AWS CDK CLI (npm install -g aws-cdk)
#   - Python 3.11+ with venv
#   - Docker (for Phase 2 image push to ECR)
#   - AgentCore CLI: pip install bedrock-agentcore-toolkit

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Load config from cdk.json
REGION=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['region'])")
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
PREFIX=$(python3 -c "import json; print(json.load(open('cdk.json'))['context'].get('stack_prefix', 'OpenClaw'))")
DOCKER_IMAGE=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['docker_image'])")

echo "╔══════════════════════════════════════════════════╗"
echo "║  OpenClaw AgentCore Deployment                   ║"
echo "║  Account: $ACCOUNT                               "
echo "║  Region:  $REGION                                "
echo "║  Image:   $DOCKER_IMAGE                          "
echo "╚══════════════════════════════════════════════════╝"

PHASE="${1:---all}"

# ── Ensure venv (uses uv from devbox) ────────────────────────────────────
if [ ! -d ".venv" ]; then
    echo "→ Creating Python virtual environment with uv..."
    uv venv --python python3
fi
source .venv/bin/activate
uv pip install -q -r requirements.txt

# ── Phase 1: Foundation ──────────────────────────────────────────────────
deploy_phase1() {
    echo ""
    echo "━━━ Phase 1: Foundation Stacks ━━━"
    cdk deploy \
        "${PREFIX}Vpc" \
        "${PREFIX}Security" \
        "${PREFIX}Guardrails" \
        "${PREFIX}Observability" \
        --require-approval never
    echo "✓ Phase 1 complete"
}

# ── Phase 2: AgentCore Runtime ───────────────────────────────────────────
deploy_phase2() {
    echo ""
    echo "━━━ Phase 2: AgentCore Runtime ━━━"

    # Get outputs from Phase 1 + Phase 3 pre-reqs
    ROLE_ARN=$(aws cloudformation describe-stacks \
        --stack-name "${PREFIX}AgentCore" \
        --query "Stacks[0].Outputs[?OutputKey=='ExecutionRoleArn'].OutputValue" \
        --output text --region "$REGION" 2>/dev/null || echo "")

    SG_ID=$(aws cloudformation describe-stacks \
        --stack-name "${PREFIX}AgentCore" \
        --query "Stacks[0].Outputs[?OutputKey=='SecurityGroupId'].OutputValue" \
        --output text --region "$REGION" 2>/dev/null || echo "")

    SUBNETS=$(aws cloudformation describe-stacks \
        --stack-name "${PREFIX}AgentCore" \
        --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnetIds'].OutputValue" \
        --output text --region "$REGION" 2>/dev/null || echo "")

    if [ -z "$ROLE_ARN" ]; then
        echo "⚠ AgentCore stack not yet deployed. Deploying it first..."
        cdk deploy "${PREFIX}AgentCore" --require-approval never
        ROLE_ARN=$(aws cloudformation describe-stacks \
            --stack-name "${PREFIX}AgentCore" \
            --query "Stacks[0].Outputs[?OutputKey=='ExecutionRoleArn'].OutputValue" \
            --output text --region "$REGION")
        SG_ID=$(aws cloudformation describe-stacks \
            --stack-name "${PREFIX}AgentCore" \
            --query "Stacks[0].Outputs[?OutputKey=='SecurityGroupId'].OutputValue" \
            --output text --region "$REGION")
        SUBNETS=$(aws cloudformation describe-stacks \
            --stack-name "${PREFIX}AgentCore" \
            --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnetIds'].OutputValue" \
            --output text --region "$REGION")
    fi

    # Pull image from Docker Hub and push to ECR
    ECR_REPO="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/openclaw-runtime"

    echo "→ Creating ECR repository (if not exists)..."
    aws ecr describe-repositories --repository-names openclaw-runtime --region "$REGION" 2>/dev/null || \
        aws ecr create-repository --repository-name openclaw-runtime --region "$REGION" \
            --image-scanning-configuration scanOnPush=true

    echo "→ Logging into ECR..."
    aws ecr get-login-password --region "$REGION" | \
        docker login --username AWS --password-stdin "$ECR_REPO"

    echo "→ Pulling $DOCKER_IMAGE from Docker Hub..."
    docker pull --platform linux/arm64 "$DOCKER_IMAGE"

    echo "→ Tagging and pushing to ECR..."
    docker tag "$DOCKER_IMAGE" "${ECR_REPO}:latest"
    docker push "${ECR_REPO}:latest"

    echo "→ Deploying AgentCore Runtime via CLI..."
    # Generate .bedrock_agentcore.yaml
    cat > .bedrock_agentcore.yaml <<EOF
agent_runtime:
  name: openclaw-agent
  image: ${ECR_REPO}:latest
  platform: linux/arm64
  role_arn: ${ROLE_ARN}
  network:
    mode: VPC
    security_groups:
      - ${SG_ID}
    subnets: [$(echo "$SUBNETS" | tr ',' ',')]
  lifecycle:
    idle_timeout: $(python3 -c "import json; print(json.load(open('cdk.json'))['context']['session_idle_timeout'])")
    max_lifetime: $(python3 -c "import json; print(json.load(open('cdk.json'))['context']['session_max_lifetime'])")
  environment:
$(python3 -c "
import json
ctx = json.load(open('cdk.json'))['context']
print(f'    S3_BUCKET: {ctx[\"stack_prefix\"].lower()}-workspaces-{\"$ACCOUNT\"}-{\"$REGION\"}')
print(f'    STACK_NAME: {ctx[\"stack_prefix\"]}')
print(f'    AWS_REGION: {ctx[\"region\"]}')
print(f'    BEDROCK_MODEL_ID: {ctx[\"default_model_id\"]}')
")
EOF

    # Deploy using agentcore CLI
    if command -v agentcore &>/dev/null; then
        agentcore deploy --config .bedrock_agentcore.yaml
    else
        echo "⚠ agentcore CLI not found. Install with: pip install bedrock-agentcore-toolkit"
        echo "  Then run: agentcore deploy --config .bedrock_agentcore.yaml"
        echo ""
        echo "  After deployment, note the runtime_id and add it to cdk.json:"
        echo '  "runtime_id": "openclaw_agent-XXXXXXXXXX"'
    fi

    echo "✓ Phase 2 complete"
    echo ""
    echo "  ⚠ Add the runtime_id to cdk.json before deploying Phase 3:"
    echo '  "runtime_id": "openclaw_agent-XXXXXXXXXX"'
}

# ── Phase 3: Application ────────────────────────────────────────────────
deploy_phase3() {
    echo ""
    echo "━━━ Phase 3: Application Stacks ━━━"
    cdk deploy \
        "${PREFIX}AgentCore" \
        "${PREFIX}Router" \
        "${PREFIX}Cron" \
        "${PREFIX}TokenMonitoring" \
        --require-approval never
    echo "✓ Phase 3 complete"

    # Print API URL
    API_URL=$(aws cloudformation describe-stacks \
        --stack-name "${PREFIX}Router" \
        --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
        --output text --region "$REGION")

    echo ""
    echo "╔══════════════════════════════════════════════════╗"
    echo "║  Deployment Complete!                            ║"
    echo "║                                                  ║"
    echo "║  API URL: $API_URL"
    echo "║                                                  ║"
    echo "║  Next steps:                                     ║"
    echo "║  1. Store channel secrets in Secrets Manager      ║"
    echo "║  2. Run setup scripts for each channel            ║"
    echo "║  3. Add users to the allowlist                    ║"
    echo "╚══════════════════════════════════════════════════╝"
}

# ── Main ─────────────────────────────────────────────────────────────────
case "$PHASE" in
    --phase1) deploy_phase1 ;;
    --phase2) deploy_phase2 ;;
    --phase3) deploy_phase3 ;;
    --all|*)
        deploy_phase1
        deploy_phase2
        deploy_phase3
        ;;
esac
