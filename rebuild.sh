#!/usr/bin/env bash
set -euo pipefail

export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
# Hash the actual client payload so uncommitted client fixes also produce a
# version change and installed clients receive the update notification.
PKGRELAY_CLIENT_VERSION=$(
  find client -type f -print0 \
    | sort -z \
    | xargs -0 sha256sum \
    | sha256sum \
    | cut -c1-12
)
export PKGRELAY_CLIENT_VERSION
docker compose build
docker compose up -d --remove-orphans
