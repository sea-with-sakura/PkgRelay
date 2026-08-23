#!/usr/bin/env bash
# Generated at /bootstrap/setenv.sh. PkgRelay owns the user's Conda sources;
# the previous user configuration is kept solely for uninstall restoration.
set -euo pipefail

PKGRELAY_URL='__PKGRELAY_URL__'
client_dir="${XDG_DATA_HOME:-$HOME/.local/share}/pkgrelay"
legacy_dir="${XDG_DATA_HOME:-$HOME/.local/share}/conda-cache"
bashrc="$HOME/.bashrc"
condarc="$HOME/.condarc"
condarc_backup="$client_dir/condarc.before"
condarc_existed="$client_dir/condarc.existed"

if [[ -t 1 ]]; then
  blue=$'\033[38;5;39m'; green=$'\033[38;5;40m'; reset=$'\033[0m'
else
  blue=''; green=''; reset=''
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

remove_old_shell_client() {
  remove_blocks
  rm -rf -- "$legacy_dir"
  # Older installers wrote this exact value. Leave every other pip setting.
  if command -v python3 >/dev/null 2>&1 && python3 -m pip --version >/dev/null 2>&1; then
    old_index=$(python3 -m pip config --user get global.index-url 2>/dev/null || true)
    [[ "$old_index" == */get/pypi/pypi/simple ]] && python3 -m pip config --user unset global.index-url >/dev/null 2>&1 || true
  fi
}

install_conda_config() {
  command -v conda >/dev/null 2>&1 || { say "未找到 conda，跳过 Conda 配置。"; return; }
  if [[ ! -e "$condarc_existed" ]]; then
    if [[ -f "$condarc" ]]; then
      cp -a "$condarc" "$condarc_backup"
      printf '1\n' > "$condarc_existed"
    else
      printf '0\n' > "$condarc_existed"
    fi
  fi
  curl -fsSL "$PKGRELAY_URL/bootstrap/client/condarc.yaml" -o "$client_dir/condarc.yaml"
  cp -f "$client_dir/condarc.yaml" "$condarc"
  conda config --file "$condarc" --validate >/dev/null
  ok "已由网关接管当前用户的 Conda Channel。"
}

restore_conda_config() {
  [[ -e "$condarc_existed" ]] || return 0
  if [[ "$(<"$condarc_existed")" == 1 ]]; then
    cp -f "$condarc_backup" "$condarc"
  else
    rm -f "$condarc"
  fi
  ok "已恢复安装前的用户 Conda 配置。"
}

install_client() {
  install -d -m 0755 "$client_dir"
  curl -fsSL "$PKGRELAY_URL/bootstrap/client/pip-wrapper.sh" -o "$client_dir/pip-wrapper.sh"
  curl -fsSL "$PKGRELAY_URL/bootstrap/client/cache-sync.sh" -o "$client_dir/cache-sync.sh"
  printf 'PKGRELAY_URL=%q\nPKGRELAY_CLIENT_DIR=%q\n' "$PKGRELAY_URL" "$client_dir" > "$client_dir/client.conf"
  chmod 0755 "$client_dir/cache-sync.sh"
  touch "$bashrc"
  remove_blocks
  cat >> "$bashrc" <<EOF

# >>> pkgrelay pip wrapper >>>
[[ -r "$client_dir/pip-wrapper.sh" ]] && source "$client_dir/pip-wrapper.sh"
# <<< pkgrelay pip wrapper <<<
EOF
  install_conda_config
  ok "已接入缓存层。"
  say "执行：exec bash -l"
}

uninstall_client() {
  remove_blocks
  restore_conda_config
  rm -rf -- "$client_dir" "$legacy_dir"
  ok "已移除缓存客户端。执行：exec bash -l"
}

say "\n${blue}== PkgRelay 缓存客户端 ==${reset}"
say "缓存站：$PKGRELAY_URL"
say "1. 安装：由网关接管 Conda / pip 缓存"
say "2. 卸载：恢复安装前的配置"
read -r -p "请选择 [1/2]：" action
case "$action" in
  1)
    remove_old_shell_client
    install_client
    ;;
  2) uninstall_client ;;
  *) say "无效选择，未做修改。" >&2; exit 2 ;;
esac
