#!/bin/bash
# ============================================================================
# LinkPilot 面板一键部署脚本（全新机器可直接运行）
#
#   curl -fsSL https://raw.githubusercontent.com/se-tang/LinkPilot/main/install.sh | bash
#
# 脚本先自检运行环境，缺什么自动装什么，再部署面板（每一步都幂等，
# 重复执行 = 升级，数据 / 端口 / 机器接入关系全部保留）。
#
# 输出风格：安静 + 进度 + 结论。只在「需要你知道」的地方说话，
# 第三方工具的刷屏输出收进日志文件（--log / LINKPILOT_LOG 可取用）。
#
# 支持的发行版：Debian/Ubuntu、CentOS/RHEL/Rocky/Alma/Fedora、Alpine、
#               openSUSE/SLES、Arch（其余系统会给出明确的失败原因）
#
# 可选环境变量（均有默认值，通常无需设置）：
#   LINKPILOT_DIR       安装目录，默认 $HOME/LinkPilot
#   LINKPILOT_REPO_URL  仓库地址，默认 https://github.com/se-tang/LinkPilot.git
#   LINKPILOT_BRANCH    分支，默认 main
#   LINKPILOT_TLS=1     同时启用 HTTPS 网关（caddy，需 80/443 空闲）
#   LINKPILOT_VERBOSE=1 把被收起的第三方命令输出一并打印出来（排障用）
#   LINKPILOT_LOG       日志文件路径，默认 /tmp/LinkPilot-install.log
#   NO_COLOR=1             关闭彩色输出
#   DOCKER_MIRROR=Aliyun   用国内镜像源装 Docker（可选值见 https://get.docker.com）
#   COMPOSE_MIRROR=...     compose 独立二进制的下载前缀（默认 GitHub 官方）
#   https_proxy=...        拉代码 / 装包走代理，例如 https_proxy=http://127.0.0.1:7890
# ============================================================================
set -u
umask 022

APP_NAME="LinkPilot"
REPO_SLUG="LinkPilot"
# 当前版本：与 app/app.py 的 APP_VERSION 保持一致（app.py 是唯一权威）。
# 升级时脚本会用「更新后的代码里」的版本号自我重跑，所以这里的值只对「全新安装」生效。
APP_VERSION="0.7.9"
REPO_URL="${LINKPILOT_REPO_URL:-https://github.com/se-tang/LinkPilot.git}"
BRANCH="${LINKPILOT_BRANCH:-main}"
TARGET_HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)"
[ -n "$TARGET_HOME" ] || TARGET_HOME="$HOME"
INSTALL_DIR="${LINKPILOT_DIR:-$TARGET_HOME/LinkPilot}"
LOG_FILE="${LINKPILOT_LOG:-/tmp/LinkPilot-install.log}"
VERBOSE="${LINKPILOT_VERBOSE:-0}"
LOCAL_BACKUP="${LINKPILOT_LOCAL_BACKUP:-}"

# ---------------------------------------------------------------------------
# 输出与通用工具
# ---------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

# 颜色：只在支持 tput 的交互终端上启用，管道 / CI / NO_COLOR 下自动退化为纯文本
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && have tput && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ] 2>/dev/null; then
  C_RESET="$(tput sgr0)"; C_BOLD="$(tput bold)"; C_DIM="$(tput dim)"
  C_RED="$(tput setaf 1)"; C_GREEN="$(tput setaf 2)"; C_YELLOW="$(tput setaf 3)"
  C_BLUE="$(tput setaf 4)"; C_CYAN="$(tput setaf 6)"; C_MAGENTA="$(tput setaf 5)"
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""
  C_BLUE=""; C_CYAN=""; C_MAGENTA=""
fi

# 交互终端才做动画：重定向 / 日志抓取（`| tee`、CI）下只打静态行，
# 免得输出里塞满 \r 刷新帧和 ANSI 转义
if [ -t 1 ] && [ -z "${LINKPILOT_PLAIN:-}" ]; then
  FANCY=1
else
  FANCY=0
fi

SPIN_PID=""
_cleanup() {
  [ -n "$SPIN_PID" ] && kill "$SPIN_PID" 2>/dev/null
  SPIN_PID=""
  # 非交互下不写控制字符：抓日志的人不会看到行尾的转义
  [ "${FANCY:-0}" = "1" ] && printf '\r\033[K' 2>/dev/null
  return 0
}
trap _cleanup EXIT INT TERM

# 文本宽度（按显示宽度近似）：中文算 2 列，其它算 1 列
_rule() { # $1=字符
  _rw=62
  _line=""
  while [ "${#_line}" -lt "$_rw" ]; do _line="$_line$1"; done
  printf '%s%s%s\n' "$C_DIM" "$_line" "$C_RESET"
}

log_file() { printf '%s\n' "$*" >>"$LOG_FILE" 2>/dev/null || true; }

# 一步的开始 / 结束：非交互环境下退化为普通的两行文字
step() {
  STEP_T0="$(date +%s 2>/dev/null || echo 0)"
  STEP_NOTE=""
  printf '  %s▸%s %s ... ' "$C_CYAN" "$C_RESET" "$1"
}
step_done() {
  # 补充说明放在结果之前输出，保证「▸ 步骤 ... 结果」永远是一行完整的信息
  [ -n "${STEP_NOTE:-}" ] && printf '%s%s%s ' "$C_DIM" "$STEP_NOTE" "$C_RESET"
  STEP_NOTE=""
  _now="$(date +%s 2>/dev/null || echo 0)"
  _ms=$(( (_now - STEP_T0) * 1000 ))
  if [ "$_ms" -lt 100 ] 2>/dev/null; then
    printf '%s%s%s %s(0.1s)%s\n' "$C_GREEN" "${1:-done}" "$C_RESET" "$C_DIM" "$C_RESET"
  elif [ "$_ms" -lt 1000 ] 2>/dev/null; then
    printf '%s%s%s %s(%.1fs)%s\n' "$C_GREEN" "${1:-done}" "$C_RESET" "$C_DIM" \
      "$(awk "BEGIN{print $_ms/1000}" 2>/dev/null || echo 0)" "$C_RESET"
  else
    printf '%s%s%s %s(%ss)%s\n' "$C_GREEN" "${1:-done}" "$C_RESET" "$C_DIM" \
      "$(( _now - STEP_T0 ))" "$C_RESET"
  fi
}
step_note() { STEP_NOTE="${STEP_NOTE:+$STEP_NOTE；}$*"; }

info() { printf '  %s·%s %s\n' "$C_DIM" "$C_RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_YELLOW$C_BOLD" "$C_RESET" "$*"; }
die() {
  _cleanup
  printf '\n  %s✗ 安装失败：%s%s\n' "$C_RED$C_BOLD" "$*" "$C_RESET" >&2
  if [ -f "$LOG_FILE" ]; then
    printf '  %s完整日志：%s%s\n' "$C_DIM" "$LOG_FILE" "$C_RESET" >&2
    printf '  %s最后 10 行：%s\n' "$C_DIM" "$C_RESET" >&2
    tail -n 10 "$LOG_FILE" 2>/dev/null | sed 's/^/    /' >&2
  fi
  exit 1
}

# 跑一条命令并把输出收进日志：失败时自动回显尾部，便于一眼定位
run_quiet() { # $@ = 命令
  log_file "\$ $*"
  if [ "$VERBOSE" = "1" ]; then
    "$@" 2>&1 | tee -a "$LOG_FILE"
    return "${PIPESTATUS[0]}"
  fi
  if "$@" >>"$LOG_FILE" 2>&1; then
    return 0
  fi
  _rc=$?
  warn "命令失败（详见 $LOG_FILE）：$*"
  tail -n 6 "$LOG_FILE" 2>/dev/null | sed 's/^/      /'
  return "$_rc"
}

# 后台运行一段耗时命令 + 前台 spinner：把「卡住了吗」变成「还在跑，已 42s」。
# $1 = 显示文字，其余为要执行的命令（在后台子 shell 里跑，stdout/stderr 全部进日志）
run_spinner() {
  _label="$1"; shift
  log_file "\$ $*"
  if [ "$VERBOSE" = "1" ]; then
    step "$_label"
    "$@" 2>&1 | tee -a "$LOG_FILE"
    _rc="${PIPESTATUS[0]}"
    [ "$_rc" = "0" ] && step_done || printf '%s失败%s\n' "$C_RED" "$C_RESET"
    return "$_rc"
  fi
  "$@" >>"$LOG_FILE" 2>&1 &
  _job=$!
  _frames='/-\|'
  _i=0
  _t0="$(date +%s 2>/dev/null || echo 0)"
  while kill -0 "$_job" 2>/dev/null; do
    _i=$(( (_i + 1) % 4 ))
    if [ "$FANCY" = "1" ]; then
      _ch="$(printf '%s' "$_frames" | cut -c$((_i + 1)))"
      _el=$(( $(date +%s 2>/dev/null || echo 0) - _t0 ))
      printf '\r  %s%s%s %s ... %s%ss%s\033[K' \
        "$C_CYAN" "$_ch" "$C_RESET" "$_label" "$C_DIM" "$_el" "$C_RESET"
    elif [ "$_i" = "1" ]; then
      printf '  %s▸%s %s ...\n' "$C_CYAN" "$C_RESET" "$_label"
    fi
    sleep 1
  done
  wait "$_job"
  _rc=$?
  [ "$FANCY" = "1" ] && printf '\r\033[K'
  if [ "$_rc" = "0" ]; then
    printf '  %s▸%s %s ... %sdone%s %s(%ss)%s\n' "$C_CYAN" "$C_RESET" "$_label" \
      "$C_GREEN" "$C_RESET" "$C_DIM" \
      "$(( $(date +%s 2>/dev/null || echo 0) - _t0 ))" "$C_RESET"
  else
    printf '  %s▸%s %s ... %s失败%s\n' "$C_CYAN" "$C_RESET" "$_label" "$C_RED$C_BOLD" "$C_RESET"
    tail -n 8 "$LOG_FILE" 2>/dev/null | sed 's/^/      /'
  fi
  return "$_rc"
}

if [ -z "${BASH_VERSION:-}" ]; then
  if have bash && [ -n "${0:-}" ] && [ -f "${0:-}" ]; then exec bash "$0" "$@"; fi
  printf '需要 bash：Debian/Ubuntu 用 apt-get install -y bash，Alpine 用 apk add bash\n' >&2
  exit 1
fi

if [ "$(id -u)" = "0" ]; then
  SUDO=""
else
  have sudo || die "需要 root 权限：请用 root 执行，或先安装 sudo（Debian/Ubuntu: apt-get install -y sudo）"
  SUDO="sudo"
fi
as_root() { if [ -n "$SUDO" ]; then $SUDO "$@"; else "$@"; fi }
docker_cli() { if [ -n "$SUDO" ]; then $SUDO docker "$@"; else docker "$@"; fi }
compose() { docker_cli compose "$@"; }

# 推导源码包地址（git 不可用时的兜底）
derive_tarball_url() {
  case "$REPO_URL" in
    https://github.com/*)
      _slug="${REPO_URL#https://github.com/}"
      _slug="${_slug%.git}"
      printf 'https://codeload.github.com/%s/tar.gz/refs/heads/%s' "$_slug" "$BRANCH" ;;
    *) printf '' ;;
  esac
}
TARBALL_URL="${LINKPILOT_TARBALL:-$(derive_tarball_url)}"

# ---------------------------------------------------------------------------
# 系统与包管理器探测
# ---------------------------------------------------------------------------
OS_PRETTY="unknown"
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release 2>/dev/null || true
  OS_PRETTY="${PRETTY_NAME:-${ID:-unknown}}"
fi
ARCH="$(uname -m 2>/dev/null || echo unknown)"

PKG="none"
for _c in apt-get dnf yum apk zypper pacman; do
  if have "$_c"; then
    case "$_c" in
      apt-get) PKG=apt ;;
      dnf) PKG=dnf ;;
      yum) PKG=yum ;;
      apk) PKG=apk ;;
      zypper) PKG=zypper ;;
      pacman) PKG=pacman ;;
    esac
    break
  fi
done

pm_update() {
  case "$PKG" in
    apt) DEBIAN_FRONTEND=noninteractive as_root apt-get update -qq ;;
    dnf) as_root dnf -q makecache ;;
    yum) as_root yum -q makecache ;;
    apk) as_root apk update -q ;;
    zypper) as_root zypper --non-interactive --quiet refresh ;;
    pacman) as_root pacman -Sy --noconfirm --quiet ;;
    *) return 1 ;;
  esac
}
pm_install() { # $@ = 当前发行版仓库里的包名
  case "$PKG" in
    apt) DEBIAN_FRONTEND=noninteractive as_root apt-get install -y --no-install-recommends "$@" ;;
    dnf) as_root dnf install -y "$@" ;;
    yum) as_root yum install -y "$@" ;;
    apk) as_root apk add --no-cache "$@" ;;
    zypper) as_root zypper --non-interactive install "$@" ;;
    pacman) as_root pacman -S --noconfirm --needed "$@" ;;
    *) return 1 ;;
  esac
}

fetch() { # $1=url $2=输出文件
  if have curl; then
    curl -fsSL --connect-timeout 15 --retry 2 -o "$2" "$1"
  elif have wget; then
    wget -q -T 15 -O "$2" "$1"
  else
    return 1
  fi
}

# --- update helpers (kept as one block for tests/test_install_update.sh) ---
ensure_runtime_excludes() {
  [ -d "$INSTALL_DIR/.git" ] || return 0
  _exclude="$INSTALL_DIR/.git/info/exclude"
  mkdir -p "$INSTALL_DIR/.git/info" 2>/dev/null || return 0
  for _pattern in '/.env' '/Caddyfile.local' '/certs/' '/data/' '/data-*/'; do
    grep -qxF "$_pattern" "$_exclude" 2>/dev/null \
      || printf '%s\n' "$_pattern" >>"$INSTALL_DIR/.git/info/exclude" 2>/dev/null || true
  done
}

repo_has_changes() {
  # .env / 证书 / 数据是运行时配置，不能被当成“本地代码改动”stash 走
  ensure_runtime_excludes
  [ -n "$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null)" ]
}

backup_repo_changes() {
  repo_has_changes || return 0
  _stash_msg="LinkPilot auto-update backup $(date +%Y%m%d-%H%M%S)"
  log_file "备份本地改动：git -C $INSTALL_DIR stash push --include-untracked -m $_stash_msg"
  if ! git -C "$INSTALL_DIR" -c user.name=LinkPilot-updater \
      -c user.email=linkpilot-updater@localhost \
      stash push --include-untracked -m "$_stash_msg" >>"$LOG_FILE" 2>&1; then
    return 1
  fi
  LOCAL_BACKUP="$(git -C "$INSTALL_DIR" rev-parse --verify refs/stash 2>/dev/null || true)"
  [ -n "$LOCAL_BACKUP" ] || return 1
  export LINKPILOT_LOCAL_BACKUP="$LOCAL_BACKUP"
  log_file "本地改动已备份到 stash $LOCAL_BACKUP"
  return 0
}

align_repo_to_remote() {
  if ! backup_repo_changes; then
    warn "本地改动备份失败，已停止更新以保护现有代码"
    return 1
  fi
  git -C "$INSTALL_DIR" checkout -q -B "$BRANCH" "origin/$BRANCH" >>"$LOG_FILE" 2>&1 \
    || git -C "$INSTALL_DIR" reset --hard -q "origin/$BRANCH" >>"$LOG_FILE" 2>&1
}

restore_runtime_from_backup() {
  [ -n "$LOCAL_BACKUP" ] || return 0
  git -C "$INSTALL_DIR" rev-parse --verify "$LOCAL_BACKUP^3" >/dev/null 2>&1 || return 0
  for _path in .env Caddyfile.local certs; do
    [ -e "$INSTALL_DIR/$_path" ] && continue
    git -C "$INSTALL_DIR" cat-file -e "$LOCAL_BACKUP^3:$_path" 2>/dev/null || continue
    git -C "$INSTALL_DIR" restore --source="$LOCAL_BACKUP^3" -- "$_path" \
      >>"$LOG_FILE" 2>&1 || continue
    log_file "已从升级备份恢复运行时配置：$_path"
  done
}
# --- end update helpers ---

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 开场：一眼看清「这是什么 / 在哪台机器上做什么」
# ---------------------------------------------------------------------------
: >"$LOG_FILE" 2>/dev/null || LOG_FILE=/dev/null

# 先做「检查更新」再印开场：走 curl | bash 时手里的是旧脚本，
# 若一上来就打印版本号，更新完成后版本号会对不上（显示的是旧版本）。
# 这里只做更新，输出全部进日志；失败/无更新都静默放过，不打断后面的安装流程。
if [ "${LINKPILOT_REEXEC:-0}" != "1" ] && have git; then
  # 从仓库目录里直接执行本脚本时，同样按该目录检查更新
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    _sd="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$_sd/docker-compose.yml" ] && [ -d "$_sd/.git" ]; then INSTALL_DIR="$_sd"; fi
  fi
fi
if [ "${LINKPILOT_REEXEC:-0}" != "1" ] && [ -d "$INSTALL_DIR/.git" ] && have git; then
  _cur_url="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
  if [ -n "$_cur_url" ] && [ "$_cur_url" != "$REPO_URL" ]; then
    git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL" >>"$LOG_FILE" 2>&1 || true
  fi
  if git -C "$INSTALL_DIR" fetch --prune origin "$BRANCH" >>"$LOG_FILE" 2>&1; then
    _l="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo '')"
    _r="$(git -C "$INSTALL_DIR" rev-parse "origin/$BRANCH" 2>/dev/null || echo '')"
    if [ -n "$_r" ] && [ "$_l" != "$_r" ] && align_repo_to_remote; then
      if [ -f "$INSTALL_DIR/install.sh" ]; then
        # 通知重跑后的脚本「本轮是刚更新上来的」，这样它的那行结论会说
        # 「发现新版本 → 已自动更新」，而不是「已是最新」（否则用户以为没生效）
        export LINKPILOT_REEXEC=1 LINKPILOT_UPDATED=1
        export LINKPILOT_DIR="$INSTALL_DIR" LINKPILOT_LOG="$LOG_FILE"
        exec bash "$INSTALL_DIR/install.sh" "$@"
      fi
    fi
  fi
fi

printf '\n'
# 版本号优先取「当前代码里的」（app.py 是唯一权威），取不到才用脚本内置值
_ver_hdr="$(sed -n "s/^APP_VERSION = '\(.*\)'/\1/p" "$INSTALL_DIR/app/app.py" 2>/dev/null | head -n1)"
printf '  %s%s%s %s%s 面板一键部署%s\n' "$C_BOLD$C_MAGENTA" "$APP_NAME" "$C_RESET" \
  "$C_DIM" "v${_ver_hdr:-$APP_VERSION}" "$C_RESET"
printf '  %s多机 iperf3 线路质量测试：目标机 + 后端机，逐台测速并生成评价报告%s\n' "$C_DIM" "$C_RESET"
_rule '─'
printf '  %s主机%s   %s%s%s (%s)\n' "$C_DIM" "$C_RESET" "$C_BOLD" "$(hostname 2>/dev/null || echo '-')" "$C_RESET" "$OS_PRETTY"
printf '  %s架构%s   %s\n' "$C_DIM" "$C_RESET" "$ARCH"
printf '  %s目录%s   %s\n' "$C_DIM" "$C_RESET" "$INSTALL_DIR"
printf '  %s日志%s   %s%s（失败时自动回显末尾）%s\n' "$C_DIM" "$C_RESET" "$C_DIM" "$LOG_FILE" "$C_RESET"
_rule '─'
printf '\n'

# ---------------------------------------------------------------------------
# 自检 1/5：下载工具与证书
# ---------------------------------------------------------------------------
step "运行环境"
ensure_downloader() {
  if have curl || have wget; then return 0; fi
  [ "$PKG" = "none" ] && return 1
  pm_update >/dev/null 2>&1 || true
  pm_install curl >/dev/null 2>&1 || pm_install wget >/dev/null 2>&1 || true
  have curl || have wget
}
if have curl || have wget; then
  step_done "curl/wget 就绪"
else
  step "安装 curl"
  run_quiet pm_update || true
  if run_quiet pm_install curl || run_quiet pm_install wget; then
    step_done "curl 已安装"
  else
    die "缺少 curl/wget 且自动安装失败，请手动安装后重试"
  fi
fi

ensure_ca() {
  [ -e /etc/ssl/certs/ca-certificates.crt ] && return 0
  [ -e /etc/pki/tls/certs/ca-bundle.crt ] && return 0
  [ -e /etc/ssl/cert.pem ] && return 0
  [ "$PKG" = "none" ] && return 0
  pm_install ca-certificates >/dev/null 2>&1 || true
  return 0
}
ensure_ca

# ---------------------------------------------------------------------------
# 自检 2/5：git
# ---------------------------------------------------------------------------
step "git"
if have git; then
  step_done "已安装 $(git --version 2>/dev/null | awk '{print $3}')"
elif [ "$PKG" = "none" ]; then
  step_done "跳过（改用源码包获取代码）"
  step_note "未识别到包管理器，升级时需重新下载源码包"
else
  run_quiet pm_update || true
  if run_quiet pm_install git && have git; then
    step_done "已安装 $(git --version 2>/dev/null | awk '{print $3}')"
  else
    step_done "跳过（改用源码包获取代码）"
    step_note "git 装不上不影响部署，只是后续升级不便"
  fi
fi

# ---------------------------------------------------------------------------
# 自检 3/5：docker 与 docker compose v2
# ---------------------------------------------------------------------------
install_docker_official() {
  _tmp="$(mktemp)"
  if ! fetch https://get.docker.com "$_tmp"; then rm -f "$_tmp"; return 1; fi
  if [ -n "${DOCKER_MIRROR:-}" ]; then
    log_file "docker 官方安装脚本（镜像源 $DOCKER_MIRROR）"
    as_root sh "$_tmp" --mirror "$DOCKER_MIRROR" >>"$LOG_FILE" 2>&1 || { rm -f "$_tmp"; return 1; }
  else
    log_file "docker 官方安装脚本 https://get.docker.com"
    as_root sh "$_tmp" >>"$LOG_FILE" 2>&1 || { rm -f "$_tmp"; return 1; }
  fi
  rm -f "$_tmp"
  have docker
}

install_docker_distro() {
  [ "$PKG" = "none" ] && return 1
  pm_update >/dev/null 2>&1 || true
  case "$PKG" in
    apt) pm_install docker.io || return 1 ;;
    dnf | yum) pm_install docker-ce || pm_install moby-engine || pm_install docker || return 1 ;;
    apk | zypper | pacman) pm_install docker || return 1 ;;
    *) return 1 ;;
  esac
  have docker
}

install_compose_binary() {
  case "$ARCH" in
    x86_64 | amd64) _carch=x86_64 ;;
    aarch64 | arm64) _carch=aarch64 ;;
    armv7l) _carch=armv7 ;;
    armv6l) _carch=armv6 ;;
    i386 | i686) _carch=i386 ;;
    *) warn "未知架构 $ARCH，跳过 compose 独立二进制安装"; return 1 ;;
  esac
  _url="https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${_carch}"
  if [ -n "${COMPOSE_MIRROR:-}" ]; then
    _url="${COMPOSE_MIRROR%/}/docker-compose-linux-${_carch}"
  fi
  _tmp="$(mktemp)"
  if ! fetch "$_url" "$_tmp"; then rm -f "$_tmp"; return 1; fi
  as_root mkdir -p /usr/local/lib/docker/cli-plugins || { rm -f "$_tmp"; return 1; }
  as_root install -m 0755 "$_tmp" /usr/local/lib/docker/cli-plugins/docker-compose || { rm -f "$_tmp"; return 1; }
  rm -f "$_tmp"
  compose version >/dev/null 2>&1
}

ensure_compose() {
  compose version >/dev/null 2>&1 && return 0
  case "$PKG" in
    apt) pm_install docker-compose-plugin >/dev/null 2>&1 || pm_install docker-compose-v2 >/dev/null 2>&1 || true ;;
    dnf | yum) pm_install docker-compose-plugin >/dev/null 2>&1 || true ;;
    apk) pm_install docker-cli-compose >/dev/null 2>&1 || true ;;
    zypper | pacman) pm_install docker-compose >/dev/null 2>&1 || true ;;
  esac
  compose version >/dev/null 2>&1 && return 0
  install_compose_binary
}

start_docker_daemon() {
  docker_cli info >/dev/null 2>&1 && return 0
  if have systemctl; then
    as_root systemctl enable --now docker >/dev/null 2>&1 || as_root systemctl start docker >/dev/null 2>&1 || true
  elif have rc-service; then
    as_root rc-service docker start >/dev/null 2>&1 || true
    as_root rc-update add docker default >/dev/null 2>&1 || true
  elif have service; then
    as_root service docker start >/dev/null 2>&1 || true
  fi
  _i=0
  while [ "$_i" -lt 30 ]; do
    docker_cli info >/dev/null 2>&1 && return 0
    _i=$((_i + 1))
    sleep 1
  done
  return 1
}

if ! have docker; then
  printf '  %s·%s 未检测到 Docker → 自动安装（官方脚本 → 发行版仓库，逐级兜底）\n' "$C_DIM" "$C_RESET"
  if ! run_spinner "安装 Docker Engine" bash -c 'install_docker_official || install_docker_distro'; then
    die "Docker 自动安装失败。请手动安装后重试（参考 https://docs.docker.com/engine/install/）"
  fi
fi
DOCKER_VER="$(docker --version 2>/dev/null | sed 's/^Docker version //;s/,.*//')"

step "Docker 守护进程"
if start_docker_daemon; then
  step_done "运行中（Engine ${DOCKER_VER:-?}）"
else
  die "Docker 守护进程启动失败：请手动执行 systemctl start docker 后重试（OpenVZ/LXC 类容器化 VPS 通常无法运行 Docker）"
fi

step "Docker Compose v2"
if compose version >/dev/null 2>&1; then
  step_done "$(compose version 2>/dev/null | head -n1 | sed 's/^Docker Compose version //')"
else
  step_note "缺失 → 发行版仓库 → 独立二进制"
  if run_quiet ensure_compose && compose version >/dev/null 2>&1; then
    step_done "$(compose version 2>/dev/null | head -n1 | sed 's/^Docker Compose version //')"
  else
    die "docker compose v2 安装失败：请手动安装 docker-compose-plugin 后重试"
  fi
fi

# ---------------------------------------------------------------------------
# 自检 4/5：获取代码 / 检查更新
#   同一个一键命令兼顾两种机器：
#     · 没装过 → 克隆仓库（git 不可用时退回源码包）
#     · 装过   → 比较本地与远端，有更新就直接更新（含历史被重写过的情况）
# ---------------------------------------------------------------------------
step "获取代码"
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/docker-compose.yml" ] && [ -d "$SELF_DIR/.git" ]; then
  INSTALL_DIR="$SELF_DIR"
fi

UPDATED=0        # 这次运行是否刚更新过代码（用于收尾文案）
REPO_LINKED=0    # INSTALL_DIR 是否是一个 git 仓库

repo_dirty() {
  [ -n "$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null)" ]
}

# 让已有目录指向本脚本配置的仓库地址（换过仓库 / 从源码包安装过的都能一次性纠正）
ensure_origin() {
  _cur="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
  if [ "$_cur" != "$REPO_URL" ]; then
    if [ -n "$_cur" ]; then
      git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL" >>"$LOG_FILE" 2>&1 || true
    else
      git -C "$INSTALL_DIR" remote add origin "$REPO_URL" >>"$LOG_FILE" 2>&1 || true
    fi
  fi
}

fetch_repo() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    REPO_LINKED=1
    ensure_origin
    log_file "检查更新：git -C $INSTALL_DIR fetch origin $BRANCH"
    if ! git -C "$INSTALL_DIR" fetch --prune origin "$BRANCH" >>"$LOG_FILE" 2>&1; then
      warn "连不上仓库，跳过更新检查，继续用当前代码部署"
      return 0
    fi
    _local="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo '')"
    _remote="$(git -C "$INSTALL_DIR" rev-parse "origin/$BRANCH" 2>/dev/null || echo '')"
    if [ -n "$_remote" ] && [ "$_local" = "$_remote" ]; then
      if [ "${LINKPILOT_UPDATED:-0}" = "1" ]; then
        CODE_STATE=updated       # 本轮的更新是在「重跑前」那一段完成的
      else
        CODE_STATE=current
      fi
      return 0
    fi
    log_file "更新到 origin/$BRANCH（本地 $_local → $_remote）"
    if align_repo_to_remote; then
      # 用 fetch + 强制对齐，而不是 git pull：远端历史被重写过（force push）时
      # pull --ff-only 会直接失败，用户就看到「有更新但升不动」
      CODE_STATE=updated
      UPDATED=1
      return 0
    fi
    if repo_dirty; then
      # 不能安全备份时绝不 reset，避免静默覆盖用户改动
      CODE_STATE=dirty
      return 0
    fi
    warn "代码更新失败，继续用当前代码部署"
    CODE_STATE=stale
    return 0
  fi
  if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    warn "$INSTALL_DIR 已存在且不是 git 仓库，直接使用该目录（升级请重新 git clone）"
    CODE_STATE=plain
    return 0
  fi
  # 全新安装
  mkdir -p "$(dirname "$INSTALL_DIR")" 2>/dev/null || true
  if have git; then
    log_file "克隆仓库 $REPO_URL（分支 $BRANCH）"
    if git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" >>"$LOG_FILE" 2>&1; then
      REPO_LINKED=1
      CODE_STATE=fresh
      return 0
    fi
    warn "git clone 失败 → 改用源码包"
  fi
  [ -n "$TARBALL_URL" ] || return 1
  log_file "下载源码包 $TARBALL_URL"
  _tmp="$(mktemp)"
  _dir="$(mktemp -d)"
  if ! fetch "$TARBALL_URL" "$_tmp"; then rm -f "$_tmp"; rm -rf "$_dir"; return 1; fi
  if ! tar -xzf "$_tmp" -C "$_dir"; then rm -f "$_tmp"; rm -rf "$_dir"; return 1; fi
  _src="$(find "$_dir" -maxdepth 1 -mindepth 1 -type d | head -n1)"
  mkdir -p "$INSTALL_DIR"
  cp -a "$_src/." "$INSTALL_DIR/" || { rm -f "$_tmp"; rm -rf "$_dir"; return 1; }
  rm -f "$_tmp"
  rm -rf "$_dir"
  CODE_STATE=tarball
  [ -f "$INSTALL_DIR/docker-compose.yml" ]
}

CODE_STATE=unknown
if [ -d "$INSTALL_DIR/.git" ] && [ "$SELF_DIR" = "$INSTALL_DIR" ]; then
  step_note "脚本位于仓库目录内，直接使用：$INSTALL_DIR"
fi

if ! run_quiet fetch_repo; then
  die "获取代码失败：请检查机器能否访问 $REPO_URL（被墙可设 https_proxy 后重试）"
fi

code_version() {
  sed -n "s/^APP_VERSION = '\(.*\)'/\1/p" "$INSTALL_DIR/app/app.py" 2>/dev/null | head -n1
}
CODE_VER="$(code_version)"
case "$CODE_STATE" in
  fresh)   step_done "已克隆仓库${CODE_VER:+（v$CODE_VER）} → $INSTALL_DIR" ;;
  tarball) step_done "已下载源码包${CODE_VER:+（v$CODE_VER）} → $INSTALL_DIR" ;;
  current) step_done "已是最新${CODE_VER:+（v$CODE_VER）}（远端无新提交）" ;;
  updated) step_done "发现新版本 → 已自动更新${CODE_VER:+（v$CODE_VER）}"
           if [ -n "$LOCAL_BACKUP" ]; then
             step_note "本地改动已备份到 stash ${LOCAL_BACKUP:0:12}"
           fi ;;
  dirty)   step_done "本地改动备份失败，未自动更新${CODE_VER:+（按当前代码 v$CODE_VER 部署）}"
           step_note "请先执行 git -C $INSTALL_DIR stash push -u，再重跑本脚本" ;;
  plain)   step_done "使用现有目录${CODE_VER:+（v$CODE_VER）}（非 git 仓库，不支持自动更新）" ;;
  *)       step_done "就绪${CODE_VER:+（v$CODE_VER）} → $INSTALL_DIR" ;;
esac

# 走管道那条命令（curl | bash）时，脚本本体是「旧的那一份」：更新完代码后
# 用更新后的脚本重新执行一次，保证本次部署用的也是最新逻辑（只重跑一次）
if [ "$UPDATED" = "1" ] && [ "${LINKPILOT_REEXEC:-0}" != "1" ] && [ -f "$INSTALL_DIR/install.sh" ]; then
  step_note "用更新后的脚本继续本次部署…"
  export LINKPILOT_REEXEC=1 LINKPILOT_DIR="$INSTALL_DIR"
  export LINKPILOT_LOG="$LOG_FILE"
  exec bash "$INSTALL_DIR/install.sh"
fi

# ---------------------------------------------------------------------------
# 自检 5/5：生成/读取 .env（端口随机生成后固定不变，升级保持不变）
# ---------------------------------------------------------------------------
restore_runtime_from_backup
step "运行配置"
rand_port() {
  if have shuf; then shuf -i 10000-30000 -n 1; return; fi
  if [ -r /dev/urandom ]; then
    od -An -N2 -tu2 /dev/urandom 2>/dev/null | tr -d ' \n' | awk '{print 10000 + ($1 % 20001)}'
    return
  fi
  printf '%s\n' "$((10000 + (RANDOM % 20001)))"
}
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1; }

# 这里判断「是不是首次部署」，必须在 docker compose 之前定死：
# compose 一起容器就会把 ./data 建出来，之后再看就永远是「已有数据」了
# （曾经因此让全新部署误报「沿用已有账号」，把该显示的一次性初始密码藏了起来）。
# 只看 auth.json：它由面板首次启动时生成，是「已经装过」的可靠标志。
if [ -f "$INSTALL_DIR/data/auth.json" ]; then
  FRESH_INSTALL=0
else
  FRESH_INSTALL=1
fi

if [ -f "$INSTALL_DIR/.env" ]; then
  step_done "沿用已有 .env（端口 / 绑定 / 域名不变）"
else
  _p=""
  _i=0
  while [ "$_i" -lt 30 ]; do
    _p="$(rand_port)"
    port_in_use "$_p" || break
    _p=""
    _i=$((_i + 1))
  done
  [ -n "$_p" ] || _p="$(rand_port)"
  printf 'PANEL_PORT=%s\n' "$_p" > "$INSTALL_DIR/.env"
  step_done "已生成 .env（随机端口 $_p）"
fi
PANEL_PORT="$(sed -n 's/^PANEL_PORT=//p' "$INSTALL_DIR/.env" | tail -n1)"
PANEL_BIND="$(sed -n 's/^PANEL_BIND=//p' "$INSTALL_DIR/.env" | tail -n1)"
PANEL_DOMAIN="$(sed -n 's/^PANEL_DOMAIN=//p' "$INSTALL_DIR/.env" | tail -n1)"
[ -n "${PANEL_PORT:-}" ] || PANEL_PORT=8088
# 面板绑定的网卡地址（空/0.0.0.0/127.0.0.1 时本机健康检查走回环）
HEALTH_HOST="127.0.0.1"
case "${PANEL_BIND:-}" in
  "" | 0.0.0.0 | 127.0.0.1 | "::" | "[::]") ;;
  *) HEALTH_HOST="$PANEL_BIND" ;;
esac

# 只检查/更新代码，不部署（想确认更新结果、或先看参数再用）：
#   curl -fsSL .../install.sh | LINKPILOT_DRY_RUN=1 bash
if [ "${LINKPILOT_DRY_RUN:-0}" = "1" ]; then
  printf '\n'
  printf '  %s--dry-run：只检查/更新代码，不部署面板（当前代码 v%s）- %s\n' \
    "$C_DIM" "${CODE_VER:-$APP_VERSION}" "$C_RESET"
  printf '  %s需要部署时去掉 LINKPILOT_DRY_RUN=1 重跑即可%s\n' "$C_DIM" "$C_RESET"
  printf '\n'
  exit 0
fi

# ---------------------------------------------------------------------------
# 部署（自动识别是否需要带 tls profile，避免升级时把 caddy 丢了）
# ---------------------------------------------------------------------------
TLS=0
[ "${LINKPILOT_TLS:-}" = "1" ] && TLS=1
tls_config_present() {
  [ -f "$INSTALL_DIR/Caddyfile.local" ] && return 0
  [ -f "$INSTALL_DIR/.env" ] || return 1
  grep -Eq '^[[:space:]]*(PANEL_DOMAIN=.+|PANEL_COOKIE_SECURE=1|CADDY_HTTP=.+|CADDY_HTTPS=.+)' \
    "$INSTALL_DIR/.env"
}
if tls_config_present; then
  TLS=1
fi
if docker_cli ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'linkpilot-caddy'; then
  TLS=1
fi
COMPOSE_ARGS=""
if [ "$TLS" = "1" ]; then
  COMPOSE_ARGS="--profile tls"
  # 历史误挂载会把 Caddyfile.local 建成目录，导致 caddy 静默启动失败
  if [ -d "$INSTALL_DIR/Caddyfile.local" ]; then
    warn "Caddyfile.local 是目录（历史误挂载残留），已清理"
    rm -rf "$INSTALL_DIR/Caddyfile.local"
  fi
  if [ ! -f "$INSTALL_DIR/Caddyfile.local" ] && [ -f "$INSTALL_DIR/Caddyfile.acme" ]; then
    cp -T "$INSTALL_DIR/Caddyfile.acme" "$INSTALL_DIR/Caddyfile.local" 2>/dev/null \
      || cp "$INSTALL_DIR/Caddyfile.acme" "$INSTALL_DIR/Caddyfile.local"
  fi
fi

printf '\n'
printf '  %s%s%s\n' "$C_BOLD" "部署面板" "$C_RESET"
printf '  %s%s%s\n' "$C_DIM" "首次部署需要构建镜像 + 拉取依赖，通常 1–3 分钟；升级会快很多" "$C_RESET"
printf '\n'

cd "$INSTALL_DIR" || die "无法进入 $INSTALL_DIR"
# shellcheck disable=SC2086
if ! run_spinner "构建并启动容器（docker compose up -d --build）" \
    compose $COMPOSE_ARGS up -d --build; then
  die "docker compose 启动失败。常见原因：磁盘空间不足、拉取基础镜像超时（可配 Docker 镜像加速）"
fi

# ---------------------------------------------------------------------------
# 等待面板就绪
# ---------------------------------------------------------------------------
step "等待面板就绪"
CID="$(compose ps -q linkpilot 2>/dev/null | head -n1)"
HEALTHY=0
_i=0
_t0="$(date +%s 2>/dev/null || echo 0)"
while [ "$_i" -lt 90 ]; do
  if [ -n "$CID" ]; then
    _st="$(docker_cli inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CID" 2>/dev/null)"
    if [ "$_st" = "healthy" ]; then HEALTHY=1; break; fi
    if [ "$_st" = "none" ] && have curl && curl -fsS -m 3 "http://$HEALTH_HOST:$PANEL_PORT/api/health" >/dev/null 2>&1; then HEALTHY=1; break; fi
    case "$_st" in exited | dead) break ;; esac
  fi
  _i=$((_i + 1))
  if [ "$FANCY" = "1" ]; then
    _ch="$(printf '/-\|' | cut -c$(( (_i % 4) + 1 )))"
    printf '\r  %s%s%s 健康检查 ... %s%ss%s\033[K' "$C_CYAN" "$_ch" "$C_RESET" "$C_DIM" \
      "$(( $(date +%s 2>/dev/null || echo 0) - _t0 ))" "$C_RESET"
  elif [ "$_i" = "1" ]; then
    printf '    %s等待容器健康检查（最长约 3 分钟）…%s\n' "$C_DIM" "$C_RESET"
  fi
  sleep 2
done
[ "$FANCY" = "1" ] && printf '\r\033[K'

if [ "$HEALTHY" != "1" ]; then
  warn "面板健康检查未通过，下面是容器日志："
  # shellcheck disable=SC2086
  compose $COMPOSE_ARGS logs --tail 30 linkpilot 2>/dev/null | sed 's/^/      /'
  die "部署未成功。常见原因：端口 $PANEL_PORT 被占用（改 .env 后重跑）、磁盘空间不足、拉取镜像超时"
fi
printf '  %s▸%s 等待面板就绪 ... %s已就绪%s %s(%ss)%s\n' "$C_CYAN" "$C_RESET" \
  "$C_GREEN" "$C_RESET" "$C_DIM" "$(( $(date +%s 2>/dev/null || echo 0) - _t0 ))" "$C_RESET"

# 抓取容器横幅（含一次性初始登录密码）：只取我们真正要展示的那几行
BANNER=""
_i=0
while [ "$_i" -lt 15 ]; do
  BANNER="$(compose $COMPOSE_ARGS logs --tail 40 linkpilot 2>/dev/null \
    | sed -n '/面板已部署成功/,/^=\+$/p' | sed 's/^linkpilot[^|]*| *//')"
  printf '%s' "$BANNER" | grep -q "面板已部署成功" && break
  _i=$((_i + 1))
  sleep 2
done

PANEL_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}')"
[ -n "${PANEL_IP:-}" ] || PANEL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${PANEL_IP:-}" ] || PANEL_IP="<面板机IP>"

_banner_field() { # $1=字段名
  printf '%s\n' "$BANNER" | sed -n "s/.*$1[:：][[:space:]]*//p" | head -n1 | tr -d '\r'
}
PANEL_USER="$(_banner_field '登录账号')"
PANEL_PW="$(_banner_field '登录密码')"

# ---------------------------------------------------------------------------
# 收尾：一屏给全「地址 / 账号 / 下一步 / 维护命令」
# ---------------------------------------------------------------------------
printf '\n'
_rule '─'
printf '  %s%s✓ 部署完成%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
printf '  %s%s v%s · 面板已就绪并随 Docker 自启%s\n' "$C_DIM" "$APP_NAME" "${CODE_VER:-$APP_VERSION}" "$C_RESET"
_rule '─'
printf '\n'
printf '  %s面板地址%s  %s%shttp://%s:%s%s\n' "$C_BOLD" "$C_RESET" "$C_BOLD$C_CYAN" "" "$PANEL_IP" "$PANEL_PORT" "$C_RESET"
if [ -n "${PANEL_DOMAIN:-}" ]; then
  printf '  %sHTTPS 域名%s  https://%s\n' "$C_BOLD" "$C_RESET" "$PANEL_DOMAIN"
fi
printf '  %s登录账号%s  %s\n' "$C_BOLD" "$C_RESET" "${PANEL_USER:-（见 $INSTALL_DIR/data/auth.json）}"
if [ -n "${PANEL_PW:-}" ] && [ "$FRESH_INSTALL" = "1" ]; then
  printf '  %s登录密码%s  %s%s%s  %s（仅本次显示，请立即保存）%s\n' "$C_BOLD" "$C_RESET" \
    "$C_BOLD$C_YELLOW" "$PANEL_PW" "$C_RESET" "$C_DIM" "$C_RESET"
elif [ "$FRESH_INSTALL" = "1" ]; then
  printf '  %s登录密码%s  %s未从容器日志中取到，请执行：docker compose logs linkpilot | grep 密码%s\n' \
    "$C_BOLD" "$C_RESET" "$C_DIM" "$C_RESET"
else
  printf '  %s登录密码%s  %s沿用已有账号（忘记密码：删除 data/auth.json 后重启容器会重新生成）%s\n' \
    "$C_BOLD" "$C_RESET" "$C_DIM" "$C_RESET"
fi
printf '\n'
printf '  %s下一步%s\n' "$C_BOLD" "$C_RESET"
printf '    1. 打开上面的面板地址，用账号密码登录\n'
printf '    2. 「添加机器」填名称 / 角色（目标机或后端机）/ 地区 / 带宽\n'
printf '    3. 复制生成的接入命令，到对应机器的 root 下执行，等它上线\n'
printf '    4. 回到面板选择目标机与后端机 → 开始测试，或在「定时任务」里挂长期对比\n'
printf '\n'
printf '  %s维护命令%s\n' "$C_BOLD" "$C_RESET"
printf '    %s升级%s  重跑本脚本即可（自动检查更新，有新版本会自己更新并重建）：\n' "$C_DIM" "$C_RESET"
printf '          curl -fsSL https://raw.githubusercontent.com/se-tang/%s/main/install.sh | bash\n' "$REPO_SLUG"
printf '    %s日志%s  cd %s && docker compose %slogs -f --tail 100 linkpilot\n' \
  "$C_DIM" "$C_RESET" "$INSTALL_DIR" "$COMPOSE_ARGS"
printf '    %s卸载%s  cd %s && docker compose %sdown && rm -rf data .env\n' \
  "$C_DIM" "$C_RESET" "$INSTALL_DIR" "$COMPOSE_ARGS"
printf '    %s安装日志%s  %s\n' "$C_DIM" "$C_RESET" "$LOG_FILE"
if [ -n "$LOCAL_BACKUP" ]; then
  printf '    %s本地备份%s  %s（查看：git -C %s stash list；恢复：git -C %s stash show -p %s）\n' \
    "$C_DIM" "$C_RESET" "${LOCAL_BACKUP:0:12}" "$INSTALL_DIR" "$INSTALL_DIR" "${LOCAL_BACKUP:0:12}"
fi
printf '\n'
_rule '─'
printf '\n'
