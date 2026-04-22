#!/bin/bash
set -e

TOKEN=$1
if [ -z "$TOKEN" ]; then
  echo "Usage: $0 <registration-token>"
  exit 1
fi

RUNNER_VERSION="2.334.0"
REPO_URL="https://github.com/suyog19/ai-dev-orchestrator"
RUNNER_DIR="/home/ubuntu/actions-runner"

echo "==> Creating runner directory"
mkdir -p "$RUNNER_DIR" && cd "$RUNNER_DIR"

echo "==> Downloading runner v${RUNNER_VERSION}"
curl -fsSL "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
  | tar xz

echo "==> Configuring runner"
./config.sh \
  --url "$REPO_URL" \
  --token "$TOKEN" \
  --name "ec2-runner" \
  --labels "self-hosted,linux,ec2" \
  --unattended \
  --replace

echo "==> Installing and starting runner as a system service"
sudo ./svc.sh install ubuntu
sudo ./svc.sh start

echo ""
echo "Runner status:"
sudo ./svc.sh status
