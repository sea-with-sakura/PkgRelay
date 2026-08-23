#!/usr/bin/env bash
# Source this file from an interactive Bash shell. Explicit external indexes
# are routed through PkgRelay while the pip/pip3 commands remain unchanged.

[[ -n "${BASH_VERSION:-}" ]] || return 0

_pkgrelay_wrapper_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
_pkgrelay_client_dir="${PKGRELAY_CLIENT_DIR:-${_pkgrelay_wrapper_dir}}"
_pkgrelay_settings="${_pkgrelay_client_dir}/client.conf"

if [[ "${PKGRELAY_IGNORE_FILE_CONFIG:-0}" != "1" && -r "${_pkgrelay_settings}" ]]; then
  # The bootstrap-generated settings contain only shell assignments.
  # shellcheck disable=SC1090
  source "${_pkgrelay_settings}"
fi

: "${PKGRELAY_URL:=http://127.0.0.1:45612}"
if [[ -r "${_pkgrelay_client_dir}/client-update.sh" ]]; then
  # shellcheck disable=SC1090
  source "${_pkgrelay_client_dir}/client-update.sh"
fi

_pkgrelay_rewrite_url() {
  local requested_url="${1%/}" token
  if [[ "${requested_url}" == "${PKGRELAY_URL%/}"/* ]]; then
    printf '%s' "${requested_url}"
    return 0
  fi
  case "${requested_url}" in
    http://*|https://*)
      token=$(printf '%s' "${requested_url}" | base64 | tr '+/' '-_' | tr -d '=\n')
      printf '%s/get/pypi/external/%s/simple' "${PKGRELAY_URL%/}" "${token}"
      ;;
    *) printf '%s' "${requested_url}" ;;
  esac
}

_pkgrelay_pip() {
  local executable="$1"
  shift
  if declare -F _pkgrelay_client_maybe_update >/dev/null; then
    _pkgrelay_client_maybe_update
  fi
  local rewritten=() argument next_url replacement
  while (($#)); do
    argument="$1"
    shift
    case "${argument}" in
      --index-url|--extra-index-url|-i)
        (($#)) || { echo "${argument} requires a URL" >&2; return 2; }
        next_url="$1"
        shift
        replacement=$(_pkgrelay_rewrite_url "${next_url}")
        rewritten+=("${argument}" "${replacement}")
        ;;
      --index-url=*|--extra-index-url=*)
        next_url="${argument#*=}"
        replacement=$(_pkgrelay_rewrite_url "${next_url}")
        rewritten+=("${argument%%=*}=${replacement}")
        ;;
      -ihttp://*|-ihttps://*)
        next_url="${argument:2}"
        replacement=$(_pkgrelay_rewrite_url "${next_url}")
        rewritten+=("-i${replacement}")
        ;;
      *) rewritten+=("${argument}") ;;
    esac
  done

  if [[ "${PKGRELAY_URL}" == http://* ]]; then
    local cache_host="${PKGRELAY_URL#http://}"
    cache_host="${cache_host%%/*}"
    cache_host="${cache_host%%:*}"
    rewritten+=(--trusted-host "${cache_host}")
  fi
  command "${executable}" "${rewritten[@]}"
}

pip() { _pkgrelay_pip pip "$@"; }
pip3() { _pkgrelay_pip pip3 "$@"; }
