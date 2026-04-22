# VM Setup — AWS EC2 (Ubuntu 22.04 LTS)

## 1. Launch EC2 Instance

In the AWS Console → EC2 → Launch Instance:

| Setting | Value |
|---------|-------|
| Name | `ai-dev-orchestrator` |
| AMI | Ubuntu Server 22.04 LTS (64-bit x86) |
| Instance type | `t3.small` (2 vCPU, 2 GB RAM) |
| Key pair | Create new → download `.pem` file, keep it safe |
| Storage | 20 GB gp3 |

## 2. Configure Security Group

Create a new security group with these inbound rules:

| Type | Port | Source | Purpose |
|------|------|--------|---------|
| SSH | 22 | Your IP | Terminal access |
| Custom TCP | 8000 | 0.0.0.0/0 | App + Jira webhook |

## 3. Connect via SSH

```bash
chmod 400 your-key.pem
ssh -i your-key.pem ubuntu@<EC2_PUBLIC_IP>
```

## 4. Run the VM Setup Script

On the EC2 instance:

```bash
# Download and run the setup script
curl -fsSL https://raw.githubusercontent.com/suyog19/ai-dev-orchestrator/main/scripts/setup-vm.sh \
  | sudo bash

# Apply docker group (no re-login needed in the same session)
newgrp docker
```

Verify:
```bash
docker --version        # Docker version 28.x.x
docker compose version  # Docker Compose version v2.x.x
```

## 5. Clone the Repo and Configure

```bash
git clone https://github.com/suyog19/ai-dev-orchestrator.git
cd ai-dev-orchestrator

cp .env.example .env
nano .env   # fill in secrets when ready (Telegram, etc.)
```

## 6. Start the Stack

```bash
docker compose up -d
```

## 7. Verify

```bash
# All three containers should be Up
docker compose ps

# Health check
curl http://localhost:8000/healthz
# Expected: {"status":"ok"}

# Confirm publicly reachable
curl http://<EC2_PUBLIC_IP>:8000/healthz
```

## Useful Commands

```bash
docker compose logs -f app     # tail app logs
docker compose down            # stop everything
docker compose up -d --build   # rebuild and restart
```
