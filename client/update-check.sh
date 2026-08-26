#!/usr/bin/env bash
# A best-effort update notice. It never blocks or changes a package command.

[[ -n "${BASH_VERSION:-}" ]] || return 0

_pkgrelay_check_client_update() {
  [[ "${PKGRELAY_CLIENT_VERSION:-}" =~ ^[A-Za-z0-9._-]+$ ]] || return 0
  command -v curl >/dev/null 2>&1 || return 0

  local now checked_at remote_version notified_file
  now=$(date +%s)
  checked_at=0
  [[ -r "${PKGRELAY_CLIENT_DIR}/update-check-at" ]] && checked_at=$(<"${PKGRELAY_CLIENT_DIR}/update-check-at")
  [[ "${checked_at}" =~ ^[0-9]+$ ]] || checked_at=0
  # A gateway update should become visible promptly, while this still avoids
  # one request per package command in a busy shell.
  (( now - checked_at >= 60 )) || return 0
  printf '%s\n' "$now" > "${PKGRELAY_CLIENT_DIR}/update-check-at" 2>/dev/null || true

  remote_version=$(curl -fsS --connect-timeout 1 --max-time 2 \
    "${PKGRELAY_URL%/}/bootstrap/client/version" 2>/dev/null | tr -d '\r\n' || true)
  [[ "${remote_version}" =~ ^[A-Za-z0-9._-]+$ ]] || return 0
  [[ "${remote_version}" != "${PKGRELAY_CLIENT_VERSION}" ]] || return 0

  notified_file="${PKGRELAY_CLIENT_DIR}/update-notified-version"
  [[ -r "$notified_file" && "$(<"$notified_file")" == "$remote_version" ]] && return 0
  printf '%s\n' "$remote_version" > "$notified_file" 2>/dev/null || true
  printf '\033[33mPkgRelay client update available. Run:\n  wget -qO /tmp/setenv.sh %s/bootstrap/setenv.sh && bash /tmp/setenv.sh && exec bash -l\033[0m\n' \
    "${PKGRELAY_URL%/}" >&2
}
