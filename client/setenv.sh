#!/usr/bin/env bash
# Generated at /bootstrap/setenv.sh. This installer only owns PkgRelay files.
set -euo pipefail

PKGRELAY_URL='__PKGRELAY_URL__'
client_dir="${XDG_DATA_HOME:-$HOME/.local/share}/pkgrelay"
legacy_dir="${XDG_DATA_HOME:-$HOME/.local/share}/conda-cache"
bashrc="$HOME/.bashrc"

if [[ -t 1 ]]; then
  blue=$'\033[38;5;39m'; green=$'\033[38;5;40m'; yellow=$'\033[38;5;220m'; reset=$'\033[0m'
else
  blue=''; green=''; yellow=''; reset=''
fi
say() { printf '%b\n' "$*"; }
ok() { say "${green}✓ $*${reset}"; }

remove_blocks() {
  [[ -f "$bashrc" ]] || return 0
  sed -i \
    -e '/# >>> pkgrelay pip wrapper >>>/,/# <<< pkgrelay pip wrapper <<</d' \
    -e '/# >>> pkgrelay conda wrapper >>>/,/# <<< pkgrelay conda wrapper <<</d' \
    -e '/# >>> conda-cache pip wrapper >>>/,/# <<< conda-cache pip wrapper <<</d' \
    -e '/# >>> conda-cache conda wrapper >>>/,/# <<< conda-cache conda wrapper <<</d' \
    "$bashrc"
}

remove_old_client() {
  remove_blocks
  rm -rf -- "$client_dir" "$legacy_dir"
  # Older installers wrote this exact value. Leave all other pip settings.
  if command -v python3 >/dev/null 2>&1 && python3 -m pip --version >/dev/null 2>&1; then
    old_index=$(python3 -m pip config --user get global.index-url 2>/dev/null || true)
    [[ "$old_index" == */get/pypi/pypi/simple ]] && python3 -m pip config --user unset global.index-url >/dev/null 2>&1 || true
  fi
}

install_client() {
  install -d -m 0755 "$client_dir"
  curl -fsSL "$PKGRELAY_URL/bootstrap/client/pip-wrapper.sh" -o "$client_dir/pip-wrapper.sh"
  curl -fsSL "$PKGRELAY_URL/bootstrap/client/conda-wrapper.sh" -o "$client_dir/conda-wrapper.sh"
  curl -fsSL "$PKGRELAY_URL/bootstrap/client/cache-sync.sh" -o "$client_dir/cache-sync.sh"
  curl -fsSL "$PKGRELAY_URL/bootstrap/client/conda-routes.conf" -o "$client_dir/conda-routes.conf"
  printf 'PKGRELAY_URL=%q\nPKGRELAY_CLIENT_DIR=%q\n' "$PKGRELAY_URL" "$client_dir" > "$client_dir/client.conf"
  chmod 0755 "$client_dir/cache-sync.sh"
  touch "$bashrc"
  remove_blocks
  cat >> "$bashrc" <<EOF

# >>> pkgrelay pip wrapper >>>
[[ -r "$client_dir/pip-wrapper.sh" ]] && source "$client_dir/pip-wrapper.sh"
# <<< pkgrelay pip wrapper <<<
# >>> pkgrelay conda wrapper >>>
[[ -r "$client_dir/conda-wrapper.sh" ]] && source "$client_dir/conda-wrapper.sh"
# <<< pkgrelay conda wrapper <<<
EOF
  ok "已接入缓存层。"
  say "执行：exec bash -l"
}

say "\n${blue}== PkgRelay 缓存客户端 ==${reset}"
say "缓存站：$PKGRELAY_URL"
say "1. 安装：接入缓存层"
say "2. 卸载：移除缓存层"
read -r -p "请选择 [1/2]：" action
case "$action" in
  1)
    read -r -p "删除旧版 PkgRelay / conda-cache 客户端？ [Y/n] " cleanup
    [[ ! "$cleanup" =~ ^[Nn]$ ]] && remove_old_client
    install_client
    ;;
  2)
    remove_old_client
    ok "已移除缓存客户端。执行：exec bash -l"
    ;;
  *) say "无效选择，未做修改。" >&2; exit 2 ;;
esac
