#!/usr/bin/env bash
# Import one host directory through a short-lived worker. The running web
# service never receives this source directory as a mount.
set -euo pipefail

usage() {
  echo "Usage: $0 <conda|pypi> <host-cache-directory> [--dry-run]" >&2
  exit 2
}

[[ $# -ge 2 && $# -le 3 ]] || usage
kind=$1
host_directory=$2
dry_run=${3:-}

[[ "$kind" == "conda" || "$kind" == "pypi" ]] || usage
[[ -d "$host_directory" ]] || { echo "Directory does not exist: $host_directory" >&2; exit 2; }
[[ -z "$dry_run" || "$dry_run" == "--dry-run" ]] || usage

host_directory=$(realpath -e "$host_directory")
arguments=(python -m app.importer /imports/source --kind "$kind")
[[ "$kind" == "pypi" ]] && arguments+=(--origin-root "$host_directory")
[[ "$dry_run" == "--dry-run" ]] && arguments+=(--dry-run)

exec docker compose run --rm --no-deps \
  -e PKGRELAY_IMPORT_ROOTS=/imports/source \
  -v "$host_directory:/imports/source:ro" \
  pkgrelay "${arguments[@]}"
