#!/usr/bin/env bash
set -euo pipefail

export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
PKGRELAY_CLIENT_VERSION=$(git log -1 --format=%H -- client | cut -c1-12)
export PKGRELAY_CLIENT_VERSION
docker compose build
docker compose down --remove-orphans
docker compose up -d
