#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y python3.12 python3-pip nodejs npm redis-server postgresql-client

echo "Install Docker Engine and Compose plugin separately."
