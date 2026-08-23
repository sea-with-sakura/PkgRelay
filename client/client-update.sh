#!/usr/bin/env bash
# Sourced by the pip and Conda wrappers.  A failed update check must never
# block an installation, so all network failures fall back to the local files.

_pkgrelay_client_maybe_update() {
  local state_dir lock_dir last_check now interval manifest remote_revision temporary
  command -v curl >/dev/null 2>&1 || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  state_dir="${_pkgrelay_client_dir:-${PKGRELAY_CLIENT_DIR:-}}/state"
  [[ -n "$state_dir" ]] || return 0
  install -d -m 0755 "$state_dir" 2>/dev/null || return 0
  now=$(date +%s)
  interval=${PKGRELAY_UPDATE_INTERVAL_SECONDS:-21600}
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=21600
  last_check=$(cat "$state_dir/last-update-check" 2>/dev/null || printf '0')
  [[ "$last_check" =~ ^[0-9]+$ ]] || last_check=0
  (( now - last_check >= interval )) || return 0

  lock_dir="$state_dir/update.lock"
  mkdir "$lock_dir" 2>/dev/null || return 0
  printf '%s\n' "$now" > "$state_dir/last-update-check"
  manifest=$(curl -fsSL --connect-timeout 1 --max-time 5 "${PKGRELAY_URL%/}/bootstrap/manifest.json" 2>/dev/null || true)
  remote_revision=$(printf '%s' "$manifest" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("revision", ""))' 2>/dev/null || true)
  if [[ -n "$remote_revision" && "$remote_revision" != "${PKGRELAY_CLIENT_REVISION:-}" ]]; then
    temporary=$(mktemp) || { rmdir "$lock_dir" 2>/dev/null || true; return 0; }
    if curl -fsSL --connect-timeout 2 --max-time 30 "${PKGRELAY_URL%/}/bootstrap/setenv.sh" -o "$temporary" \
      && bash "$temporary" --update >/dev/null 2>&1; then
      printf '%s\n' "$remote_revision" > "$state_dir/last-updated-revision"
      PKGRELAY_CLIENT_REVISION="$remote_revision"
    fi
    rm -f -- "$temporary"
  fi
  rmdir "$lock_dir" 2>/dev/null || true
}
