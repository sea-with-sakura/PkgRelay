#!/usr/bin/env bash
# pip still chooses versions. PkgRelay supplies the transport cache only.

[[ -n "${BASH_VERSION:-}" ]] || return 0
_pkgrelay_wrapper_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
_pkgrelay_client_dir="${PKGRELAY_CLIENT_DIR:-${_pkgrelay_wrapper_dir}}"
[[ -r "${_pkgrelay_client_dir}/client.conf" ]] && source "${_pkgrelay_client_dir}/client.conf"
: "${PKGRELAY_URL:=http://127.0.0.1:45612}"

_pkgrelay_rewrite_pypi_url() {
  local requested_url="${1%/}" token
  [[ "${requested_url}" == "${PKGRELAY_URL%/}"/* ]] && { printf '%s' "${requested_url}"; return; }
  case "${requested_url}" in
    http://*|https://*)
      token=$(printf '%s' "${requested_url}" | base64 | tr '+/' '-_' | tr -d '=\n')
      printf '%s/get/pypi/external/%s/simple' "${PKGRELAY_URL%/}" "${token}"
      ;;
    *) printf '%s' "${requested_url}" ;;
  esac
}

_pkgrelay_pip() {
  local executable="$1"; shift
  local rewritten=() argument next_url replacement has_index=0 subcommand="${1:-}"
  while (($#)); do
    argument="$1"; shift
    case "${argument}" in
      --index-url|--extra-index-url|-i)
        (($#)) || { echo "${argument} requires a URL" >&2; return 2; }
        next_url="$1"; shift; replacement=$(_pkgrelay_rewrite_pypi_url "${next_url}")
        rewritten+=("${argument}" "${replacement}"); [[ "${argument}" != "--extra-index-url" ]] && has_index=1
        ;;
      --index-url=*|--extra-index-url=*)
        next_url="${argument#*=}"; replacement=$(_pkgrelay_rewrite_pypi_url "${next_url}")
        rewritten+=("${argument%%=*}=${replacement}"); [[ "${argument}" == --index-url=* ]] && has_index=1
        ;;
      -ihttp://*|-ihttps://*)
        replacement=$(_pkgrelay_rewrite_pypi_url "${argument:2}"); rewritten+=("-i${replacement}"); has_index=1
        ;;
      *) rewritten+=("${argument}") ;;
    esac
  done
  case "${subcommand}" in
    install|download|wheel) [[ "${has_index}" == 1 ]] || rewritten+=(--index-url "${PKGRELAY_URL%/}/get/pypi/pypi/simple") ;;
  esac
  if [[ "${PKGRELAY_URL}" == http://* ]]; then
    local cache_host="${PKGRELAY_URL#http://}"; cache_host="${cache_host%%/*}"; cache_host="${cache_host%%:*}"
    rewritten+=(--trusted-host "${cache_host}")
  fi
  command "${executable}" "${rewritten[@]}"
}

pip() { _pkgrelay_pip pip "$@"; }
pip3() { _pkgrelay_pip pip3 "$@"; }
