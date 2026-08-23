#!/usr/bin/env bash
# Source from an interactive Bash shell after Conda's own shell hook. Package
# operations use PkgRelay; activation commands retain Conda's shell behavior.

[[ -n "${BASH_VERSION:-}" ]] || return 0

_pkgrelay_wrapper_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
_pkgrelay_client_dir="${PKGRELAY_CLIENT_DIR:-${_pkgrelay_wrapper_dir}}"

if [[ "${PKGRELAY_IGNORE_FILE_CONFIG:-0}" != "1" \
  && -r "${_pkgrelay_client_dir}/client.conf" ]]; then
  # The bootstrap-generated settings contain only shell assignments.
  # shellcheck disable=SC1090
  source "${_pkgrelay_client_dir}/client.conf"
fi

: "${PKGRELAY_URL:=http://127.0.0.1:45612}"
_pkgrelay_conda_channels="${_pkgrelay_client_dir}/conda-channels.conf"
if [[ -r "${_pkgrelay_client_dir}/client-update.sh" ]]; then
  # shellcheck disable=SC1090
  source "${_pkgrelay_client_dir}/client-update.sh"
fi

_pkgrelay_has_override_channels() {
  local argument
  for argument in "$@"; do
    [[ "${argument}" == "--override-channels" ]] && return 0
  done
  return 1
}

_pkgrelay_has_explicit_channel() {
  local argument
  for argument in "$@"; do
    case "${argument}" in -c|--channel|--channel=*) return 0 ;; esac
  done
  return 1
}

if declare -F conda >/dev/null 2>&1 && ! declare -F _pkgrelay_saved_conda >/dev/null 2>&1; then
  _pkgrelay_function_definition="$(declare -f conda)"
  _pkgrelay_function_definition="${_pkgrelay_function_definition/#conda /_pkgrelay_saved_conda }"
  eval "${_pkgrelay_function_definition}"
  unset _pkgrelay_function_definition
fi

_pkgrelay_run_conda() {
  local command_name="$1"
  shift
  if declare -F _pkgrelay_client_maybe_update >/dev/null; then
    _pkgrelay_client_maybe_update
  fi
  local -a channels=() arguments=("${command_name}" "$@")
  local channel

  if _pkgrelay_has_explicit_channel "${arguments[@]}"; then
    if _pkgrelay_has_override_channels "${arguments[@]}"; then
      _pkgrelay_saved_conda "${arguments[@]}"
    else
      _pkgrelay_saved_conda "${arguments[@]}" --override-channels
    fi
    return
  fi
  if [[ ! -r "${_pkgrelay_conda_channels}" ]]; then
    echo "pkgrelay: missing ${_pkgrelay_conda_channels}; run the user bootstrap again." >&2
    return 2
  fi
  while IFS= read -r channel || [[ -n "${channel}" ]]; do
    [[ -n "${channel}" && "${channel}" != \#* ]] && channels+=("${channel}")
  done < "${_pkgrelay_conda_channels}"
  if ((${#channels[@]} == 0)); then
    echo "pkgrelay: no cache channels are configured; run the user bootstrap again." >&2
    return 2
  fi

  for channel in "${channels[@]}"; do
    arguments+=(--channel "${channel}")
  done
  _pkgrelay_saved_conda "${arguments[@]}" --override-channels
}

conda() {
  case "${1:-}" in
    create|install|update|upgrade|search|repoquery) _pkgrelay_run_conda "$@" ;;
    *) _pkgrelay_saved_conda "$@" ;;
  esac
}
