#!/usr/bin/env bash
# Source from an interactive Bash shell after Conda's own shell hook.  Conda
# continues to decide channels, priority, and packages; package operations are
# only routed through PkgRelay while activation commands retain their normal
# shell behavior.

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

if declare -F conda >/dev/null 2>&1 && ! declare -F _pkgrelay_saved_conda >/dev/null 2>&1; then
  _pkgrelay_function_definition="$(declare -f conda)"
  _pkgrelay_function_definition="${_pkgrelay_function_definition/#conda /_pkgrelay_saved_conda }"
  eval "${_pkgrelay_function_definition}"
  unset _pkgrelay_function_definition
fi

_pkgrelay_encode_url() {
  printf '%s' "$1" | base64 | tr '+/' '-_' | tr -d '=\n'
}

_pkgrelay_conda_route() {
  local upstream="${1%/}" token
  if [[ "${upstream}" == "${PKGRELAY_URL%/}"/* ]]; then
    printf '%s' "${upstream}"
    return
  fi
  token=$(_pkgrelay_encode_url "${upstream}")
  printf '%s/get/conda/external/%s' "${PKGRELAY_URL%/}" "${token}"
}

_pkgrelay_conda_python() {
  if [[ -n "${CONDA_PYTHON_EXE:-}" && -x "${CONDA_PYTHON_EXE}" ]]; then
    printf '%s' "${CONDA_PYTHON_EXE}"
  else
    command -v python3
  fi
}

_pkgrelay_channel_roots_from_urls() {
  "$( _pkgrelay_conda_python )" -c '
import json
import re
import sys
from urllib.parse import urlsplit, urlunsplit

platform = re.compile(r"^(?:noarch|linux-[^/]+|osx-[^/]+|win-[^/]+|emscripten-[^/]+|zos-[^/]+)$")
seen = set()
for value in json.load(sys.stdin):
    parsed = urlsplit(value)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and platform.match(parts[-1]):
        parts.pop()
    root = urlunsplit((parsed.scheme, parsed.netloc, "/" + "/".join(parts) if parts else "", "", "")).rstrip("/")
    if root and root not in seen:
        seen.add(root)
        print(root)
'
}

_pkgrelay_effective_conda_roots() {
  # ``conda info`` is Conda's own resolved view of all rc files, channel
  # aliases, custom channels and defaults.  We deliberately do not read or
  # write .condarc ourselves.
  _pkgrelay_saved_conda info --json 2>/dev/null |
    "$( _pkgrelay_conda_python )" -c 'import json, sys; print(json.dumps(json.load(sys.stdin).get("channels", [])))' |
    _pkgrelay_channel_roots_from_urls
}

_pkgrelay_explicit_conda_roots() {
  # Let Conda resolve a channel name such as ``conda-forge`` or ``defaults``.
  # This honours the user's channel_alias, custom_channels and default_channels.
  local requested="$1"
  "$( _pkgrelay_conda_python )" - "$requested" <<'PY'
from conda.models.channel import Channel
from urllib.parse import urlsplit, urlunsplit
import re
import sys

platform = re.compile(r"^(?:noarch|linux-[^/]+|osx-[^/]+|win-[^/]+|emscripten-[^/]+|zos-[^/]+)$")
seen = set()
for value in Channel(sys.argv[1]).urls():
    parsed = urlsplit(value)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and platform.match(parts[-1]):
        parts.pop()
    root = urlunsplit((parsed.scheme, parsed.netloc, "/" + "/".join(parts) if parts else "", "", "")).rstrip("/")
    if root and root not in seen:
        seen.add(root)
        print(root)
PY
}

_pkgrelay_add_conda_routes() {
  local root route
  while IFS= read -r root || [[ -n "${root}" ]]; do
    [[ -z "${root}" ]] && continue
    route=$(_pkgrelay_conda_route "${root}")
    printf '%s\n' "${route}"
  done
}

_pkgrelay_run_conda() {
  local command_name="$1"
  shift
  if declare -F _pkgrelay_client_maybe_update >/dev/null; then
    _pkgrelay_client_maybe_update
  fi
  local -a arguments=("${command_name}") roots=() routes=()
  local argument channel explicit=0
  while (($#)); do
    argument="$1"
    shift
    case "${argument}" in
      -c|--channel)
        (($#)) || { echo "${argument} requires a channel" >&2; return 2; }
        channel="$1"
        shift
        explicit=1
        while IFS= read -r argument || [[ -n "${argument}" ]]; do roots+=("${argument}"); done < <(_pkgrelay_explicit_conda_roots "${channel}")
        ;;
      --channel=*)
        channel="${argument#*=}"
        explicit=1
        while IFS= read -r argument || [[ -n "${argument}" ]]; do roots+=("${argument}"); done < <(_pkgrelay_explicit_conda_roots "${channel}")
        ;;
      *) arguments+=("${argument}") ;;
    esac
  done

  # Explicit -c values are prepended by Conda; the configured channels still
  # participate unless --override-channels was requested.  Build precisely
  # that effective list, then use temporary gateway URLs only for this call.
  if [[ "${explicit}" == 0 ]] || ! _pkgrelay_has_override_channels "${arguments[@]}"; then
    while IFS= read -r argument || [[ -n "${argument}" ]]; do roots+=("${argument}"); done < <(_pkgrelay_effective_conda_roots)
  fi
  if ((${#roots[@]} == 0)); then
    echo "pkgrelay: Conda did not report an effective channel; command was not changed." >&2
    _pkgrelay_saved_conda "${arguments[@]}"
    return
  fi
  while IFS= read -r argument || [[ -n "${argument}" ]]; do routes+=("${argument}"); done < <(printf '%s\n' "${roots[@]}" | _pkgrelay_add_conda_routes)
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
