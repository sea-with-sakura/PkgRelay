#!/usr/bin/env bash
# Keep Conda's package cache transient.  Staging lives below Conda's own
# installation root so normal environments can use fast hard links during a
# transaction; the staging directory is removed afterwards.

[[ -n "${BASH_VERSION:-}" ]] || return 0
_pkgrelay_conda_wrapper_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
_pkgrelay_client_dir="${PKGRELAY_CLIENT_DIR:-${_pkgrelay_conda_wrapper_dir}}"
[[ -r "${_pkgrelay_client_dir}/client.conf" ]] && source "${_pkgrelay_client_dir}/client.conf"
[[ -r "${_pkgrelay_client_dir}/update-check.sh" ]] && source "${_pkgrelay_client_dir}/update-check.sh"
_pkgrelay_conda_exe="${CONDA_EXE:-$(type -P conda || true)}"
if [[ -n "$_pkgrelay_conda_exe" && -x "$_pkgrelay_conda_exe" ]]; then
  _pkgrelay_conda_base="$(cd -- "$(dirname -- "$_pkgrelay_conda_exe")/.." && pwd -P)"
else
  _pkgrelay_conda_base=""
fi
if [[ -n "$_pkgrelay_conda_base" && -d "$_pkgrelay_conda_base/pkgs" ]]; then
  _pkgrelay_conda_pkgs="$_pkgrelay_conda_base/pkgs/.pkgrelay-staging"
else
  _pkgrelay_conda_pkgs="${XDG_CACHE_HOME:-$HOME/.cache}/pkgrelay/conda-pkgs"
fi

if ! declare -F _pkgrelay_base_conda >/dev/null 2>&1; then
  if declare -F conda >/dev/null 2>&1; then
    eval "$(declare -f conda | sed '1s/^conda /_pkgrelay_base_conda /')"
  else
    _pkgrelay_base_conda() { command conda "$@"; }
  fi
fi

_pkgrelay_conda_changes_packages() {
  case "${1:-}" in
    create|install|update|upgrade|remove|uninstall) return 0 ;;
    env)
      case "${2:-}" in create|update|remove) return 0 ;; esac
      ;;
  esac
  return 1
}

_pkgrelay_clear_conda_staging() {
  case "$_pkgrelay_conda_pkgs" in
    */pkgs/.pkgrelay-staging|"${XDG_CACHE_HOME:-$HOME/.cache}"/pkgrelay/conda-pkgs)
      rm -rf -- "$_pkgrelay_conda_pkgs"
      ;;
  esac
}

conda() {
  if declare -F _pkgrelay_check_client_update >/dev/null; then
    _pkgrelay_check_client_update || return $?
  fi
  if ! _pkgrelay_conda_changes_packages "$@"; then
    _pkgrelay_base_conda "$@"
    return $?
  fi

  _pkgrelay_clear_conda_staging
  mkdir -p -- "$_pkgrelay_conda_pkgs"
  local status
  if CONDA_ALWAYS_COPY=false CONDA_PKGS_DIRS="$_pkgrelay_conda_pkgs" _pkgrelay_base_conda "$@"; then
    status=0
  else
    status=$?
  fi
  _pkgrelay_clear_conda_staging
  return "$status"
}
