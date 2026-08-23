#!/usr/bin/env bash
# Synchronise a user's package archives to the shared cache before pruning
# local download caches. Installed environments are never removed here.
set -euo pipefail

[[ "${1:-}" == "--prune" || "${1:-}" == "--sync" || -z "${1:-}" ]] || {
  echo "Usage: cache-sync.sh [--sync|--prune]" >&2
  exit 2
}
prune=0
if [[ "${1:-}" == "--prune" ]]; then
  prune=1
fi

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
pruned=0
conda_dirs=()
pip_archives=()
conda_archives=()
total=0
processed=0
progress_action="准备中"
progress_enabled=0
if [[ -t 1 ]]; then
  progress_enabled=1
fi

render_progress() {
  local archive=${1:-} filename percent filled empty bar position
  [[ "$progress_enabled" == 1 && "$total" -gt 0 ]] || return 0
  filename=$(basename -- "$archive")
  percent=$((processed * 100 / total))
  position=$((processed + 1))
  [[ "$position" -le "$total" ]] || position=$total
  filled=$((processed * 28 / total))
  empty=$((28 - filled))
  bar=$(printf '%*s' "$filled" '' | tr ' ' '#')
  bar+=$(printf '%*s' "$empty" '' | tr ' ' '.')
  printf '\r\033[K同步缓存 [%s] %d/%d %3d%% · %s · %s' "$bar" "$position" "$total" "$percent" "$progress_action" "$filename"
}

show_action() {
  local action=$1 archive=$2
  progress_action=$action
  if [[ "$progress_enabled" == 1 ]]; then
    render_progress "$archive"
  else
    printf 'cache-sync: %s · %s (%d/%d)\n' "$action" "$(basename -- "$archive")" "$((processed + 1))" "$total"
  fi
}

complete_archive() {
  local archive=$1
  ((processed += 1))
  render_progress "$archive"
}

print_error() {
  if [[ "$progress_enabled" == 1 ]]; then
    printf '\n' >&2
  fi
  printf '%s\n' "$*" >&2
}

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
  local kind=$1 archive=$2 origin=${3:-} sha filename origin_token encoded status result=0
  show_action "检查网关" "$archive"
  sha=$(sha256sum -- "$archive" | awk '{print $1}')
  filename=$(basename -- "$archive")
  origin_token=$(token "$origin")
  encoded=$(query "$filename" "$origin_token")
  status=$(curl -sS -o "$tmp_reply" -w '%{http_code}' \
    "${PKGRELAY_URL%/}/api/v1/client-sync/${kind}/${sha}?${encoded}" || true)
  if [[ "$status" == "200" ]]; then
    ((present += 1))
    if [[ "$prune" == 1 ]]; then
      rm -f -- "$archive"
      ((pruned += 1))
      show_action "网关已有，已清理" "$archive"
    else
      show_action "网关已有" "$archive"
    fi
  elif [[ "$status" != "404" ]]; then
    print_error "cache-sync: check failed: $archive (HTTP $status)"
    ((failed += 1))
    result=1
  else
    show_action "上传中" "$archive"
    status=$(curl -sS -o "$tmp_reply" -w '%{http_code}' -X PUT \
      --data-binary @"$archive" \
      "${PKGRELAY_URL%/}/api/v1/client-sync/${kind}/${sha}?${encoded}" || true)
    if [[ "$status" == "201" ]]; then
      ((synced += 1))
      if [[ "$prune" == 1 ]]; then
        rm -f -- "$archive"
        ((pruned += 1))
        show_action "已上传并清理" "$archive"
      else
        show_action "已上传" "$archive"
      fi
    else
      print_error "cache-sync: upload failed: $archive (HTTP $status)"
      ((failed += 1))
      result=1
    fi
  fi
  complete_archive "$archive"
  return "$result"
}

discover_archives() {
  local pip_cache archive cache_dir
  pip_cache=$(python3 -m pip cache dir 2>/dev/null || true)
  if [[ -d "$pip_cache" ]]; then
    while IFS= read -r -d '' archive; do
      pip_archives+=("$archive")
    done < <(find "$pip_cache" -type f \( -name '*.whl' -o -name '*.tar.gz' -o -name '*.tar.bz2' -o -name '*.tar.xz' -o -name '*.zip' \) -print0)
  fi
  if command -v conda >/dev/null 2>&1; then
    while IFS= read -r cache_dir; do
      [[ "$cache_dir" == "$HOME/"* && -d "$cache_dir" ]] || continue
      conda_dirs+=("$cache_dir")
      while IFS= read -r -d '' archive; do
        conda_archives+=("$archive")
      done < <(find "$cache_dir" -maxdepth 1 -type f \( -name '*.conda' -o -name '*.tar.bz2' \) -print0)
    done < <(conda info --json | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("pkgs_dirs", [])))')
  fi
  total=$((${#pip_archives[@]} + ${#conda_archives[@]}))
}

sync_pip_cache() {
  local archive origin
  for archive in "${pip_archives[@]}"; do
    origin=$(pip_origin "$archive")
    sync_archive pip "$archive" "$origin" || true
  done
  if [[ "$prune" == 1 && "$failed" == 0 ]]; then
    python3 -m pip cache purge >/dev/null 2>&1 || true
  fi
}

sync_conda_cache() {
  local archive origin cache_dir
  for archive in "${conda_archives[@]}"; do
    cache_dir=$(dirname -- "$archive")
    origin=$(conda_origin "$cache_dir" "$(basename -- "$archive")" || true)
    sync_archive conda "$archive" "$origin" || true
  done
  if [[ "$prune" == 1 && "$failed" == 0 ]]; then
    for cache_dir in "${conda_dirs[@]}"; do
      rm -rf -- "$cache_dir/cache" "$cache_dir/urls" "$cache_dir/urls.txt"
    done
  fi
}

printf '\n== PkgRelay 本地缓存同步 ==\n'
if [[ "$prune" == 1 ]]; then
  printf '模式：上传到网关，并清理“网关已有或刚上传成功”的本地归档。不会删除任何 Conda 环境或 pip 已安装包。\n'
else
  printf '模式：仅上传，不删除本地归档。\n'
fi
printf '正在扫描当前用户的 pip / Conda 下载缓存…\n'
discover_archives
printf '发现：Conda %d 个 · pip %d 个 · 合计 %d 个归档\n' "${#conda_archives[@]}" "${#pip_archives[@]}" "$total"
if [[ "$total" == 0 ]]; then
  printf '没有可同步的下载归档。\n'
fi
sync_pip_cache
sync_conda_cache
if [[ "$progress_enabled" == 1 && "$total" -gt 0 ]]; then
  printf '\n'
fi
printf '完成：网关已有 %d 个 · 新上传 %d 个 · 失败 %d 个' "$present" "$synced" "$failed"
if [[ "$prune" == 1 ]]; then
  printf ' · 已清理本地归档 %d 个' "$pruned"
fi
printf '\n'
[[ "$failed" == 0 ]]
