#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

echo "==> Updating system packages"
apt-get update && apt-get upgrade -y \
  -o Dpkg::Options::="--force-confdef" \
  -o Dpkg::Options::="--force-confold"

echo "==> Installing prerequisites"
apt-get install -y ca-certificates curl gnupg git

echo "==> Installing Docker"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "==> Adding ubuntu user to docker group"
usermod -aG docker ubuntu

echo "==> Enabling Docker on boot"
systemctl enable docker
systemctl start docker

echo ""
echo "Done. Versions:"
docker --version
docker compose version
echo ""
echo "Next: run 'newgrp docker' so group change takes effect."
