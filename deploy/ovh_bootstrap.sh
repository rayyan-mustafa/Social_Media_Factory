#!/usr/bin/env bash
# OVHcloud VPS bootstrap (Ubuntu 22.04/24.04)
# Run as a sudo-capable user after cloning the repo.
set -euo pipefail

sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-v2 git curl
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

echo "Log out/in for docker group, then:"
echo "  cp .env.example .env   # fill secrets"
echo "  mkdir -p secrets"
echo "  docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build"
echo "  curl http://localhost:8000/health"
