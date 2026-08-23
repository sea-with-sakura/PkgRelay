#!/usr/bin/env bash
# Keep Conda's package cache transient. Environments use copied files, so
# their contents never depend on this directory after a transaction finishes.

[[ -n "${BASH_VERSION:-}" ]] || return 0
_pkgrelay_conda_root="${XDG_CACHE_HOME:-$HOME/.cache}/pkgrelay"
_pkgrelay_conda_pkgs="${_pkgrelay_conda_root}/conda-pkgs"

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
    "${_pkgrelay_conda_root}"/conda-pkgs) rm -rf -- "$_pkgrelay_conda_pkgs" ;;
  esac
}

conda() {
  if ! _pkgrelay_conda_changes_packages "$@"; then
    _pkgrelay_base_conda "$@"
    return $?
  fi

  _pkgrelay_clear_conda_staging
  mkdir -p -- "$_pkgrelay_conda_pkgs"
  local status
  if CONDA_PKGS_DIRS="$_pkgrelay_conda_pkgs" _pkgrelay_base_conda "$@"; then
    status=0
  else
    status=$?
  fi
  _pkgrelay_clear_conda_staging
  return "$status"
}
