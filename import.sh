#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname -- "$0")"

conda_cache=${1:-/data_panel/pkgrelay/imports/172.16.8.251/conda-pkgs}
pip_cache=${2:-/data_panel/pkgrelay/imports/172.16.8.251/pip-cache}

echo "Preview import:"
./import/import-cache.sh conda "$conda_cache" --dry-run
./import/import-cache.sh pypi "$pip_cache" --dry-run

echo
read -r -p "Proceed with import? [y/N] " answer
[[ "$answer" =~ ^[Yy]$ ]] || exit 0

./import/import-cache.sh conda "$conda_cache"
./import/import-cache.sh pypi "$pip_cache"
