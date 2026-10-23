#!/bin/bash
# 在 WSL 的 Ubuntu 里做首次环境配置（重启后由我调用，也可你自己跑）。
#
#   bash wsl-bootstrap.sh
#
# 做这些事（全部可重复执行，已配好的会跳过）：
#   1. 用户 deepseek：存在性检查 + sudo 免密
#   2. apt 源换成阿里云镜像（你在国内，官方源会很慢）
#   3. 常用工具：python3/pip、git、curl、wget、jq、unzip、openssh-client、build-essential、net-tools、iperf3
#   4. 打开 systemd（WSL 支持；这样 systemctl / docker 都正常）
#   5. 装 Docker（走阿里云镜像源；用来跑本文档里的 LinkPilot 面板容器）
set -u

# 目标用户名：优先用环境变量；否则读 /etc/wsl.conf 里的默认用户；再否则取第一个普通用户。
# 这样万一首次启动时你填了别的名字，也不用改脚本。
detect_user() {
  if [ -f /etc/wsl.conf ]; then
    u="$(sed -n 's/^default=//p' /etc/wsl.conf | head -1)"
    [ -n "$u" ] && [ "$u" != "root" ] && { echo "$u"; return; }
  fi
  for d in /home/*; do
    [ -d "$d" ] || continue
    echo "${d#/home/}"; return
  done
  echo ''
}
USER_NAME="${WSL_USER:-$(detect_user)}"
USER_NAME="${USER_NAME:-deepseek}"
APT_MIRROR="${APT_MIRROR:-mirrors.aliyun.com}"
DOCKER_MIRROR="${DOCKER_MIRROR:-Aliyun}"
PASS="${WSL_PASS:-}"      # 可选：传了就同步该用户的登录密码（sudo 免密与本项无关）

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }

if [ "$(id -u)" != "0" ]; then
  bad "请用 root 运行：sudo bash $0"
  exit 1
fi
say "环境：$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}") / $(uname -m)"

# ---------------------------------------------------------------- 1. 用户
say "1/5 用户与 sudo"
if id "$USER_NAME" >/dev/null 2>&1; then
  ok "用户 $USER_NAME 已存在"
else
  warn "用户 $USER_NAME 不存在，创建中（无密码登录，仅用于 WSL）"
  adduser --disabled-password --gecos "" "$USER_NAME" >/dev/null
  ok "已创建用户 $USER_NAME"
fi
if [ -d "/home/$USER_NAME" ]; then
  usermod -aG sudo "$USER_NAME" 2>/dev/null || true
  usermod -aG adm  "$USER_NAME" 2>/dev/null || true
  printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$USER_NAME" > "/etc/sudoers.d/90-$USER_NAME"
  chmod 0440 "/etc/sudoers.d/90-$USER_NAME"
  # 可选：用 WSL_PASS 同步登录密码（不传就保留现有密码）
  if [ -n "$PASS" ]; then
    echo "$USER_NAME:$PASS" | chpasswd && ok "已同步登录密码"
  fi
  # WSL 默认登录用户（已存在 [user] 段时要把 default 改成目标用户）
  touch /etc/wsl.conf
  if grep -q '^default=' /etc/wsl.conf; then
    sed -i "s|^default=.*|default=$USER_NAME|" /etc/wsl.conf
  elif grep -q '^\[user\]' /etc/wsl.conf; then
    sed -i "/^\[user\]/a default=$USER_NAME" /etc/wsl.conf
  else
    printf '\n[user]\ndefault=%s\n' "$USER_NAME" >> /etc/wsl.conf
  fi
  ok "已给 $USER_NAME 配置 sudo 免密，并设为 WSL 默认用户"
fi

# ---------------------------------------------------------------- 2. apt 镜像
say "2/5 apt 源 → $APT_MIRROR"
CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
  # Ubuntu 24.04+ 用 deb822 格式
  cp -n /etc/apt/sources.list.d/ubuntu.sources "/etc/apt/sources.list.d/ubuntu.sources.bak" 2>/dev/null || true
  {
    printf 'Types: deb\nURIs: https://%s/ubuntu/\nSuites: %s %s-updates %s-backports\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n\n' \
      "$APT_MIRROR" "$CODENAME" "$CODENAME" "$CODENAME"
    printf 'Types: deb\nURIs: https://%s/ubuntu/\nSuites: %s-security\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
      "$APT_MIRROR" "$CODENAME"
  } > /etc/apt/sources.list.d/ubuntu.sources
  ok "已写入 deb822 格式镜像源（$CODENAME）"
elif [ -f /etc/apt/sources.list ]; then
  cp -n /etc/apt/sources.list /etc/apt/sources.list.bak 2>/dev/null || true
  sed -i -E "s|https?://(archive|security)\.ubuntu\.com/ubuntu|https://$APT_MIRROR/ubuntu|g" /etc/apt/sources.list
  ok "已替换 sources.list 里的官方源"
else
  warn "没找到 apt 源文件，跳过"
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq 2>&1 | tail -2 || warn "apt-get update 有报错，稍后可重试"
ok "apt 索引已更新"

# ------------------------------------------------------------ 3. 常用工具
say "3/5 常用工具"
PKGS="python3 python3-pip python3-venv git curl wget jq unzip zip openssh-client \
build-essential ca-certificates net-tools iproute2 dnsutils iperf3 ripgrep less \
tmux htop rsync"
MISSING=""
for p in $PKGS; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
done
if [ -n "$MISSING" ]; then
  echo "  待安装：$MISSING"
  apt-get install -y -qq $MISSING >/dev/null 2>&1 || warn "部分包安装失败，可重跑本脚本"
fi
ok "工具就绪：python3 $(python3 -V 2>&1 | awk '{print $2}') / git $(git --version 2>/dev/null | awk '{print $3}') / iperf3 $(iperf3 --version 2>/dev/null | head -1 | awk '{print $2}')"

# ---------------------------------------------------------------- 4. systemd
say "4/5 systemd"
if grep -q '^systemd=true' /etc/wsl.conf 2>/dev/null; then
  ok 'systemd 已开启'
else
  if grep -q '^\[boot\]' /etc/wsl.conf 2>/dev/null; then
    sed -i '/^\[boot\]/a systemd=true' /etc/wsl.conf
  else
    printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf
  fi
  ok '已写入 systemd=true（需要 wsl --shutdown 后重进才生效）'
fi
echo '  /etc/wsl.conf 当前内容：'
sed 's/^/    /' /etc/wsl.conf

# ---------------------------------------------------------------- 5. Docker
say "5/5 Docker（供跑 LinkPilot 面板用）"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ok "Docker 已安装：$(docker --version 2>/dev/null)"
else
  if curl -fsSL --connect-timeout 15 https://get.docker.com -o /tmp/get-docker.sh 2>/dev/null; then
    if sh /tmp/get-docker.sh --mirror "$DOCKER_MIRROR" >/tmp/docker-install.log 2>&1; then
      ok "Docker 安装完成：$(docker --version 2>/dev/null)"
    else
      warn "Docker 安装失败（日志 /tmp/docker-install.log 末尾）："
      tail -n 5 /tmp/docker-install.log | sed 's/^/    /'
    fi
    rm -f /tmp/get-docker.sh
  else
    warn "拉取 Docker 安装脚本失败（网络问题），可稍后重跑本脚本"
  fi
fi
if command -v docker >/dev/null 2>&1; then
  usermod -aG docker "$USER_NAME" 2>/dev/null || true
  ok "已把 $USER_NAME 加入 docker 组（免 sudo 用 docker，重进 shell 生效）"
fi

# Docker Hub 在国内直连不通（registry-1.docker.io 超时），必须配加速站，
# 否则 iperf3-fleet 拉 python:3.12-slim / caddy 会失败。可用性已实测：
#   docker.m.daocloud.io / docker.1ms.run / docker.1panel.live  → 通
#   dockerproxy.net / hub.rat.dev / 中科大 / 腾讯云（仅内网）   → 不通
if command -v docker >/dev/null 2>&1; then
  say "Docker 镜像加速（国内必需）"
  mkdir -p /etc/docker
  if [ -f /etc/docker/daemon.json ] && [ -s /etc/docker/daemon.json ]; then
    cp /etc/docker/daemon.json "/etc/docker/daemon.json.bak.$(date +%s)"
    warn "已备份原 daemon.json"
  fi
  cat > /etc/docker/daemon.json <<'JSON'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run",
    "https://docker.1panel.live"
  ],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
  ok "已写入 3 个加速站 + 日志轮转（防容器日志撑爆磁盘）"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl restart docker 2>/dev/null || service docker restart 2>/dev/null || true
  fi
  sleep 3
  if timeout 120 docker pull alpine:3.20 >/tmp/pull.log 2>&1; then
    ok "加速站验证通过（alpine 拉取成功）"
  else
    warn "拉取验证失败，日志末尾："; tail -3 /tmp/pull.log | sed 's/^/    /'
  fi
fi

say "完成"
cat <<EOF
  用户        : $USER_NAME（sudo 免密，WSL 默认登录用户）
  apt 镜像    : $APT_MIRROR
  常用工具    : python3 / git / curl / jq / iperf3 / rg 等
  systemd     : 已开启（需在 Windows 侧执行 wsl --shutdown 后重进）
  Docker      : $(command -v docker >/dev/null 2>&1 && docker --version || echo '未装成功，可重跑本脚本')

  验证方式（在 Windows PowerShell 里）：
    wsl -d Ubuntu -- bash -lc "sudo -n true && echo sudo-ok; python3 -V; git --version; iperf3 --version | head -1"
EOF
