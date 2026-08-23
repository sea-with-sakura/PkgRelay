#!/usr/bin/env bash
set -euo pipefail

export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  export PKGRELAY_RELEASE=$(git rev-parse --short HEAD)
fi
docker compose up --build -d
