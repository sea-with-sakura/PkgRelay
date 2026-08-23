#!/usr/bin/env bash
# Synchronise a user's package archives to the shared cache before pruning
# local download caches.  Installed environments are never removed here.
set -euo pipefail

[[ "${1:-}" == "--prune" || "${1:-}" == "--sync" || -z "${1:-}" ]] || {
  echo "Usage: cache-sync.sh [--sync|--prune]" >&2
  exit 2
}
prune=0
[[ "${1:-}" == "--prune" ]] && prune=1

client_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -r "$client_dir/client.conf" ]]; then
  # shellcheck disable=SC1090
  source "$client_dir/client.conf"
fi
: "${PKGRELAY_URL:=http://127.0.0.1:45612}"

command -v curl >/dev/null || { echo "cache-sync: curl is required" >&2; exit 2; }
command -v sha256sum >/dev/null || { echo "cache-sync: sha256sum is required" >&2; exit 2; }
command -v python3 >/dev/null || { echo "cache-sync: python3 is required" >&2; exit 2; }

tmp_reply=$(mktemp)
trap 'rm -f "$tmp_reply"' EXIT
synced=0
present=0
failed=0
conda_dirs=()

token() {
  printf '%s' "$1" | base64 | tr '+/' '-_' | tr -d '=\n'
}

query() {
  python3 - "$1" "$2" <<'PY'
import sys
from urllib.parse import urlencode
print(urlencode({"filename": sys.argv[1], "origin": sys.argv[2]}))
PY
}

pip_origin() {
  local archive=$1 origin_json
  origin_json="$(dirname "$archive")/origin.json"
  [[ -r "$origin_json" ]] || return 0
  python3 - "$origin_json" <<'PY'
import json
import sys
try:
    value = json.load(open(sys.argv[1])).get("url", "")
    if isinstance(value, str):
        print(value)
except Exception:
    pass
PY
}

conda_origin() {
  local cache_dir=$1 filename=$2 urls_file url value
  urls_file="$cache_dir/urls.txt"
  [[ -r "$urls_file" ]] || return 0
  while IFS= read -r url; do
    value=${url%%\?*}
    value=${value##*/}
    [[ "$value" == "$filename" ]] && { printf '%s' "$url"; return 0; }
  done < "$urls_file"
}

sync_archive() {
  local kind=$1 archive=$2 origin=${3:-} sha filename origin_token encoded status
  sha=$(sha256sum -- "$archive" | awk '{print $1}')
  filename=$(basename -- "$archive")
  origin_token=$(token "$origin")
  encoded=$(query "$filename" "$origin_token")
  status=$(curl -sS -o "$tmp_reply" -w '%{http_code}' \
    "${PKGRELAY_URL%/}/api/v1/client-sync/${kind}/${sha}?${encoded}" || true)
  if [[ "$status" == "200" ]]; then
    ((present += 1))
    [[ "$prune" == 1 ]] && rm -f -- "$archive"
    return 0
  fi
  if [[ "$status" != "404" ]]; then
    echo "cache-sync: check failed: $archive (HTTP $status)" >&2
    ((failed += 1))
    return 1
  fi
  status=$(curl -sS -o "$tmp_reply" -w '%{http_code}' -X PUT \
    --data-binary @"$archive" \
    "${PKGRELAY_URL%/}/api/v1/client-sync/${kind}/${sha}?${encoded}" || true)
  if [[ "$status" == "201" ]]; then
    ((synced += 1))
    [[ "$prune" == 1 ]] && rm -f -- "$archive"
    return 0
  fi
  echo "cache-sync: upload failed: $archive (HTTP $status)" >&2
  ((failed += 1))
  return 1
}

sync_pip_cache() {
  local pip_cache archive origin
  pip_cache=$(python3 -m pip cache dir 2>/dev/null || true)
  [[ -d "$pip_cache" ]] || return 0
  while IFS= read -r -d '' archive; do
    origin=$(pip_origin "$archive")
    sync_archive pip "$archive" "$origin" || true
  done < <(find "$pip_cache" -type f \( -name '*.whl' -o -name '*.tar.gz' -o -name '*.tar.bz2' -o -name '*.tar.xz' -o -name '*.zip' \) -print0)
  if [[ "$prune" == 1 && "$failed" == 0 ]]; then
    python3 -m pip cache purge >/dev/null 2>&1 || true
  fi
}

sync_conda_cache() {
  local cache_dir archive origin
  command -v conda >/dev/null 2>&1 || return 0
  while IFS= read -r cache_dir; do
    [[ "$cache_dir" == "$HOME/"* && -d "$cache_dir" ]] || continue
    conda_dirs+=("$cache_dir")
    while IFS= read -r -d '' archive; do
      origin=$(conda_origin "$cache_dir" "$(basename -- "$archive")")
      sync_archive conda "$archive" "$origin" || true
    done < <(find "$cache_dir" -maxdepth 1 -type f \( -name '*.conda' -o -name '*.tar.bz2' \) -print0)
  done < <(conda info --json | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("pkgs_dirs", [])))')
  if [[ "$prune" == 1 && "$failed" == 0 ]]; then
    for cache_dir in "${conda_dirs[@]}"; do
      rm -rf -- "$cache_dir/cache" "$cache_dir/urls" "$cache_dir/urls.txt"
    done
  fi
}

sync_pip_cache
sync_conda_cache
echo "cache-sync: central already had $present, uploaded $synced, failed $failed"
[[ "$failed" == 0 ]]
