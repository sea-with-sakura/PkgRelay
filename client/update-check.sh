#!/usr/bin/env bash
# Atomically refresh the installed shell client when the gateway version changes.

[[ -n "${BASH_VERSION:-}" ]] || return 0

_pkgrelay_check_client_update() {
  if [[ ! "${PKGRELAY_CLIENT_VERSION:-}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    printf 'PkgRelay client version is missing; package command blocked.\n' >&2
    return 42
  fi
  if ! command -v curl >/dev/null 2>&1; then
    printf 'PkgRelay cannot check for updates because curl is unavailable.\n' >&2
    return 42
  fi

  local remote_version update_dir file machine username config_tmp
  remote_version=$(curl -fsS --connect-timeout 1 --max-time 2 \
    "${PKGRELAY_URL%/}/bootstrap/client/version" 2>/dev/null | tr -d '\r\n' || true)
  if [[ ! "${remote_version}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    printf 'PkgRelay cannot verify the gateway client version; package command blocked.\n' >&2
    return 42
  fi
  [[ "${remote_version}" != "${PKGRELAY_CLIENT_VERSION}" ]] || return 0

  update_dir=$(mktemp -d "${PKGRELAY_CLIENT_DIR}/.update.XXXXXX") || return 42
  for file in pip-wrapper.sh conda-wrapper.sh cache-sync.sh update-check.sh; do
    if ! curl -fsSL "${PKGRELAY_URL%/}/bootstrap/client/${file}" \
      -o "${update_dir}/${file}"; then
      rm -rf -- "$update_dir"
      printf 'PkgRelay automatic client update failed; package command blocked.\n' >&2
      return 42
    fi
    if ! bash -n "${update_dir}/${file}"; then
      rm -rf -- "$update_dir"
      printf 'PkgRelay rejected an invalid client update; package command blocked.\n' >&2
      return 42
    fi
  done

  for file in pip-wrapper.sh conda-wrapper.sh update-check.sh; do
    install -m 0644 "${update_dir}/${file}" "${PKGRELAY_CLIENT_DIR}/${file}"
  done
  install -m 0755 "${update_dir}/cache-sync.sh" "${PKGRELAY_CLIENT_DIR}/cache-sync.sh"
  rm -rf -- "$update_dir"

  machine=$(hostname -s 2>/dev/null | tr -cd 'A-Za-z0-9._-' | cut -c1-64)
  username=$(printf '%s' "${USER:-user}" | tr -cd 'A-Za-z0-9._-' | cut -c1-64)
  machine=${machine:-host}
  username=${username:-user}
  if ! curl -fsS -X POST -G "${PKGRELAY_URL%/}/api/v1/clients/register" \
    --data-urlencode "client_id=${PKGRELAY_CLIENT_ID}" \
    --data-urlencode "machine=${machine}" \
    --data-urlencode "username=${username}" \
    --data-urlencode "client_version=${remote_version}" >/dev/null; then
    printf 'PkgRelay updated local files but could not register the new version; package command blocked.\n' >&2
    return 42
  fi

  config_tmp="${PKGRELAY_CLIENT_DIR}/client.conf.update"
  printf 'PKGRELAY_URL=%q\nPKGRELAY_CLIENT_DIR=%q\nPKGRELAY_CLIENT_ID=%q\nPKGRELAY_CLIENT_VERSION=%q\n' \
    "$PKGRELAY_URL" "$PKGRELAY_CLIENT_DIR" "$PKGRELAY_CLIENT_ID" "$remote_version" \
    > "$config_tmp"
  chmod 0600 "$config_tmp"
  mv -f -- "$config_tmp" "${PKGRELAY_CLIENT_DIR}/client.conf"
  PKGRELAY_CLIENT_VERSION="$remote_version"
  export PKGRELAY_CLIENT_VERSION
  printf '\033[32mPkgRelay client automatically updated to %s.\033[0m\n' "$remote_version" >&2
  return 0
}
