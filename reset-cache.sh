#!/usr/bin/env bash
# Remove only PkgRelay's bind-mounted cache contents. This is intentionally
# explicit because it deletes cached package data and the metadata database.
set -euo pipefail

[[ "${1:-}" == "--yes" && $# == 1 ]] || {
  echo "Usage: ./reset-cache.sh --yes" >&2
  exit 2
}

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cache_dir=/data_panel/pkgrelay/cache
[[ -f "$project_dir/docker-compose.yml" ]] || { echo "Run from the PkgRelay project." >&2; exit 2; }
[[ -d "$cache_dir" ]] || { echo "Cache directory does not exist: $cache_dir" >&2; exit 0; }

docker compose -f "$project_dir/docker-compose.yml" down
# Package files are written by the service container and can therefore be
# root-owned on the host. Use the already-built service image to remove only
# this mounted directory, without requiring host sudo privileges.
docker run --rm -v "$cache_dir:/data" --entrypoint sh pkgrelay:dev -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'
mkdir -p "$cache_dir"
echo "Removed PkgRelay cache data: $cache_dir"
echo "Start the service with: ./rebuild.sh"
