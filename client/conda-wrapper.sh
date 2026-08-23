#!/usr/bin/env bash
# PkgRelay only changes the transport of recognised public Conda channels.
# Conda continues to resolve channels, dependencies and package versions.

[[ -n "${BASH_VERSION:-}" ]] || return 0

_pkgrelay_wrapper_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
_pkgrelay_client_dir="${PKGRELAY_CLIENT_DIR:-${_pkgrelay_wrapper_dir}}"
[[ -r "${_pkgrelay_client_dir}/client.conf" ]] && source "${_pkgrelay_client_dir}/client.conf"
: "${PKGRELAY_URL:=http://127.0.0.1:45612}"

if declare -F conda >/dev/null 2>&1 && ! declare -F _pkgrelay_saved_conda >/dev/null 2>&1; then
  _pkgrelay_function_definition="$(declare -f conda)"
  _pkgrelay_function_definition="${_pkgrelay_function_definition/#conda /_pkgrelay_saved_conda }"
  eval "${_pkgrelay_function_definition}"
  unset _pkgrelay_function_definition
fi

_pkgrelay_conda_python() {
  if [[ -n "${CONDA_PYTHON_EXE:-}" && -x "${CONDA_PYTHON_EXE}" ]]; then
    printf '%s' "${CONDA_PYTHON_EXE}"
  else
    command -v python3
  fi
}

_pkgrelay_channel_roots_from_urls() {
  "$(_pkgrelay_conda_python)" -c '
import json, re, sys
from urllib.parse import urlsplit, urlunsplit
platform = re.compile(r"^(?:noarch|linux-[^/]+|osx-[^/]+|win-[^/]+|emscripten-[^/]+|zos-[^/]+)$")
seen = set()
for value in json.load(sys.stdin):
    parsed = urlsplit(value)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and platform.match(parts[-1]): parts.pop()
    root = urlunsplit((parsed.scheme, parsed.netloc, "/" + "/".join(parts) if parts else "", "", "")).rstrip("/")
    if root and root not in seen:
        seen.add(root); print(root)
'
}

_pkgrelay_effective_conda_roots() {
  _pkgrelay_saved_conda info --json 2>/dev/null |
    "$(_pkgrelay_conda_python)" -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get("channels", [])))' |
    _pkgrelay_channel_roots_from_urls
}

_pkgrelay_explicit_conda_roots() {
  "$(_pkgrelay_conda_python)" - "$1" <<'PY'
from conda.models.channel import Channel
import re, sys
from urllib.parse import urlsplit, urlunsplit
platform = re.compile(r"^(?:noarch|linux-[^/]+|osx-[^/]+|win-[^/]+|emscripten-[^/]+|zos-[^/]+)$")
seen = set()
for value in Channel(sys.argv[1]).urls():
    parsed = urlsplit(value)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and platform.match(parts[-1]): parts.pop()
    root = urlunsplit((parsed.scheme, parsed.netloc, "/" + "/".join(parts) if parts else "", "", "")).rstrip("/")
    if root and root not in seen:
        seen.add(root); print(root)
PY
}

_pkgrelay_conda_route() {
  local upstream="${1%/}" route_file root route
  route_file="${_pkgrelay_client_dir}/conda-routes.conf"
  [[ -r "${route_file}" ]] || { printf '%s' "${upstream}"; return; }
  while IFS=$'\t' read -r root route; do
    [[ "${root%/}" == "${upstream}" ]] && { printf '%s' "${route}"; return; }
  done < "${route_file}"
  # Private and unknown channels remain untouched rather than becoming a
  # second, dynamic proxy configuration.
  printf '%s' "${upstream}"
}

_pkgrelay_has_override_channels() {
  local argument
  for argument in "$@"; do [[ "${argument}" == "--override-channels" ]] && return 0; done
  return 1
}

_pkgrelay_run_conda() {
  local command_name="$1"; shift
  local -a arguments=("${command_name}") roots=() routes=()
  local argument channel explicit=0
  while (($#)); do
    argument="$1"; shift
    case "${argument}" in
      -c|--channel)
        (($#)) || { echo "${argument} requires a channel" >&2; return 2; }
        channel="$1"; shift; explicit=1
        while IFS= read -r argument || [[ -n "${argument}" ]]; do roots+=("${argument}"); done < <(_pkgrelay_explicit_conda_roots "${channel}")
        ;;
      --channel=*)
        explicit=1; channel="${argument#*=}"
        while IFS= read -r argument || [[ -n "${argument}" ]]; do roots+=("${argument}"); done < <(_pkgrelay_explicit_conda_roots "${channel}")
        ;;
      *) arguments+=("${argument}") ;;
    esac
  done
  if [[ "${explicit}" == 0 ]] || ! _pkgrelay_has_override_channels "${arguments[@]}"; then
    while IFS= read -r argument || [[ -n "${argument}" ]]; do roots+=("${argument}"); done < <(_pkgrelay_effective_conda_roots)
  fi
  ((${#roots[@]})) || { _pkgrelay_saved_conda "${arguments[@]}"; return; }
  for channel in "${roots[@]}"; do routes+=("$(_pkgrelay_conda_route "${channel}")"); done
  for channel in "${routes[@]}"; do arguments+=(--channel "${channel}"); done
  _pkgrelay_has_override_channels "${arguments[@]}" || arguments+=(--override-channels)
  _pkgrelay_saved_conda "${arguments[@]}"
}

conda() {
  case "${1:-}" in
    create|install|update|upgrade|search|repoquery) _pkgrelay_run_conda "$@" ;;
    *) _pkgrelay_saved_conda "$@" ;;
  esac
}
