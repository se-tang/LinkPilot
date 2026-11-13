#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git init --bare -q -b main "$TMP/remote.git"
git init -q -b main "$TMP/seed"
git -C "$TMP/seed" -c user.name=test -c user.email=test@example.com \
  -c commit.gpgsign=false commit --allow-empty -q -m init
printf 'remote-v1\n' >"$TMP/seed/app.txt"
git -C "$TMP/seed" add app.txt
git -C "$TMP/seed" -c user.name=test -c user.email=test@example.com \
  -c commit.gpgsign=false commit -q -m v1
git -C "$TMP/seed" remote add origin "$TMP/remote.git"
git -C "$TMP/seed" push -q -u origin main

git clone -q "$TMP/remote.git" "$TMP/work"
printf 'local-change\n' >>"$TMP/work/app.txt"
printf 'untracked-change\n' >"$TMP/work/local-only.txt"
printf 'PANEL_PORT=23456\nPANEL_DOMAIN=panel.example.com\n' >"$TMP/work/.env"
printf 'custom-caddy-config\n' >"$TMP/work/Caddyfile.local"
mkdir -p "$TMP/work/certs" "$TMP/work/data"
printf 'origin-cert\n' >"$TMP/work/certs/origin.pem"
printf 'runtime-db\n' >"$TMP/work/data/panel.db"

printf 'remote-v2\n' >"$TMP/seed/app.txt"
git -C "$TMP/seed" add app.txt
git -C "$TMP/seed" -c user.name=test -c user.email=test@example.com \
  -c commit.gpgsign=false commit -q -m v2
git -C "$TMP/seed" push -q

sed -n '/^# --- update helpers/,/^# --- end update helpers/p' "$ROOT/install.sh" \
  >"$TMP/helpers.sh"

INSTALL_DIR="$TMP/work"
BRANCH=main
LOG_FILE="$TMP/update.log"
warn() { printf '%s\n' "$*" >&2; }
log_file() { printf '%s\n' "$*" >>"$LOG_FILE"; }
# shellcheck disable=SC1091
. "$TMP/helpers.sh"

git -C "$INSTALL_DIR" fetch -q origin "$BRANCH"
align_repo_to_remote

test "$(git -C "$INSTALL_DIR" rev-parse HEAD)" = \
     "$(git -C "$INSTALL_DIR" rev-parse "origin/$BRANCH")"
test "$(cat "$INSTALL_DIR/app.txt")" = "remote-v2"
test -z "$(git -C "$INSTALL_DIR" status --porcelain)"
test "$(cat "$INSTALL_DIR/.env")" = "PANEL_PORT=23456
PANEL_DOMAIN=panel.example.com"
test "$(cat "$INSTALL_DIR/Caddyfile.local")" = "custom-caddy-config"
test "$(cat "$INSTALL_DIR/certs/origin.pem")" = "origin-cert"
test "$(cat "$INSTALL_DIR/data/panel.db")" = "runtime-db"
test -n "$LOCAL_BACKUP"
test "$(git -C "$INSTALL_DIR" stash list | wc -l)" -eq 1
git -C "$INSTALL_DIR" show "$LOCAL_BACKUP:app.txt" | grep -q 'local-change'
git -C "$INSTALL_DIR" show "$LOCAL_BACKUP^3:local-only.txt" | grep -q 'untracked-change'
! git -C "$INSTALL_DIR" cat-file -e "$LOCAL_BACKUP^3:.env" 2>/dev/null
! git -C "$INSTALL_DIR" cat-file -e "$LOCAL_BACKUP^3:certs/origin.pem" 2>/dev/null

# 模拟旧版脚本已经把运行时文件 stash 走：新版启动时必须自动找回。
git clone -q "$TMP/remote.git" "$TMP/legacy"
printf 'PANEL_PORT=11192\nPANEL_BIND=127.0.0.1\nPANEL_DOMAIN=panel.example.com\n' \
  >"$TMP/legacy/.env"
printf 'legacy-caddy\n' >"$TMP/legacy/Caddyfile.local"
mkdir -p "$TMP/legacy/certs"
printf 'legacy-cert\n' >"$TMP/legacy/certs/origin.pem"
git -C "$TMP/legacy" -c user.name=test -c user.email=test@example.com \
  -c commit.gpgsign=false stash push -u -q -m "linkpilot auto-update backup test"
LOCAL_BACKUP="$(git -C "$TMP/legacy" rev-parse refs/stash)"
rm -f "$TMP/legacy/.env" "$TMP/legacy/Caddyfile.local"
rm -rf "$TMP/legacy/certs"
INSTALL_DIR="$TMP/legacy"
restore_runtime_from_backup
grep -qx 'PANEL_PORT=11192' "$INSTALL_DIR/.env"
grep -qx 'PANEL_BIND=127.0.0.1' "$INSTALL_DIR/.env"
test "$(cat "$INSTALL_DIR/Caddyfile.local")" = "legacy-caddy"
test "$(cat "$INSTALL_DIR/certs/origin.pem")" = "legacy-cert"

# 非交互日志只打印一次步骤开始，不能随着内部计时循环反复刷屏。
sed -n '/^run_spinner() {/,/^}/p' "$ROOT/install.sh" >"$TMP/run_spinner.sh"
(
  C_RESET=""; C_CYAN=""; C_GREEN=""; C_RED=""; C_BOLD=""; C_DIM=""
  FANCY=0
  VERBOSE=0
  LOG_FILE="$TMP/spinner.log"
  log_file() { :; }
  step() { :; }
  step_done() { :; }
  warn() { :; }
  # shellcheck disable=SC1091
  . "$TMP/run_spinner.sh"
  SPIN_OUT="$(run_spinner 'spinner-test' sleep 5)"
  test "$(printf '%s\n' "$SPIN_OUT" | grep -c '▸ spinner-test ...')" -eq 2
)

printf 'install.sh update / non-interactive log OK\n'

# 公网 IP 探测必须校验 IPv4，不能用上游错误页或异常文本污染访问地址。
sed -n '/^detect_public_ip() {/,/^}/p' "$ROOT/install.sh" >"$TMP/public-ip.sh"
mkdir -p "$TMP/public-ip-bin"
cat >"$TMP/public-ip-bin/curl" <<'CURL_EOF'
#!/bin/sh
printf '203.0.113.42\n'
CURL_EOF
chmod +x "$TMP/public-ip-bin/curl"
(
  # shellcheck disable=SC1091
  . "$TMP/public-ip.sh"
  detected="$(PATH="$TMP/public-ip-bin:$PATH" detect_public_ip)"
  test "$detected" = "203.0.113.42"
)
printf 'install.sh public IP detection OK\n'

# 通过 stdin 跑完整脚本，模拟尚未安装 Docker 的机器。
# PATH 只包含显式允许的工具，安装命令和 Docker 均使用替身。
mkdir -p "$TMP/bootstrap/bin" "$TMP/bootstrap/panel"
BOOTSTRAP_BIN="$TMP/bootstrap/bin"
for tool in awk bash cat chmod cut date getent grep head ln mktemp od rm sed sh sleep tail tr uname; do
  ln -s "$(command -v "$tool")" "$BOOTSTRAP_BIN/$tool"
done
touch "$TMP/bootstrap/panel/docker-compose.yml"
cat >"$BOOTSTRAP_BIN/id" <<'STUB'
#!/bin/sh
echo 0
STUB
cat >"$BOOTSTRAP_BIN/curl" <<'STUB'
#!/bin/sh
while [ "$1" != "-o" ]; do shift; done
[ "$3" = https://get.docker.com ] || exit 2
printf 'official:download\n' >>"$CALLS"
[ "$SCENARIO" != download_fail ] || exit 22
cat >"$2" <<'OFFICIAL'
printf 'official:run %s\n' "$*" >>"$CALLS"
[ "$SCENARIO" = official ] || exit 1
ln -s "$MOCK_DOCKER" "$BOOTSTRAP_BIN/docker"
OFFICIAL
STUB
cat >"$BOOTSTRAP_BIN/apt-get" <<'STUB'
#!/bin/sh
printf 'apt:%s\n' "$*" >>"$CALLS"
case "$1 $*" in
  'update '*) exit 0 ;;
  'install '*' docker.io')
    [ "$SCENARIO" != both_fail ] || exit 1
    ln -s "$MOCK_DOCKER" "$BOOTSTRAP_BIN/docker" ;;
  *) exit 2 ;;
esac
STUB
cat >"$TMP/bootstrap/docker-mock" <<'STUB'
#!/bin/sh
case "$*" in
  --version) echo 'Docker version test' ;;
  info) exit 0 ;;
  'compose version') echo 'Docker Compose version v2.test' ;;
  *) exit 1 ;;
esac
STUB
chmod +x "$BOOTSTRAP_BIN"/{id,curl,apt-get} "$TMP/bootstrap/docker-mock"

for scenario in official official_fail download_fail both_fail; do
  rm -f "$BOOTSTRAP_BIN/docker"
  calls="$TMP/bootstrap/$scenario.calls"
  log="$TMP/bootstrap/$scenario.log"
  out="$TMP/bootstrap/$scenario.out"
  touch "$calls"
  verbose=0
  if [ "$scenario" = official_fail ]; then verbose=1; fi
  rc=0
  cat "$ROOT/install.sh" | env -u BASH_ENV PATH="$BOOTSTRAP_BIN" \
    BOOTSTRAP_BIN="$BOOTSTRAP_BIN" MOCK_DOCKER="$TMP/bootstrap/docker-mock" \
    SCENARIO="$scenario" CALLS="$calls" DOCKER_MIRROR=test-mirror \
    LINKPILOT_DIR="$TMP/bootstrap/panel" LINKPILOT_LOG="$log" \
    LINKPILOT_DRY_RUN=1 LINKPILOT_PLAIN=1 LINKPILOT_VERBOSE="$verbose" \
    bash >"$out" 2>&1 || rc=$?
  if [ "$scenario" = both_fail ]; then
    test "$rc" -eq 1
    grep -q 'Docker 自动安装失败' "$out"
  elif [ "$rc" -ne 0 ]; then
    cat "$out" "$log" >&2
    exit "$rc"
  else
    grep -q -- '--dry-run' "$out"
  fi
  grep -qx 'official:download' "$calls"
  if [ "$scenario" = download_fail ]; then
    ! grep -q '^official:run ' "$calls"
  else
    grep -qx 'official:run --mirror test-mirror' "$calls"
  fi
  if [ "$scenario" = official ]; then
    ! grep -q '^apt:' "$calls"
  else
    grep -qx 'apt:install -y --no-install-recommends docker.io' "$calls"
    grep -q '尝试发行版软件源' "$log"
  fi
  ! grep -q 'command not found' "$log"
done

# 安装命令的端口/绑定覆盖必须直接写入 .env，并保持 Agent 内部监听只绑回环。
runtime_dir="$TMP/bootstrap/runtime-overrides"
mkdir -p "$runtime_dir"
touch "$runtime_dir/docker-compose.yml"
calls="$TMP/bootstrap/runtime-overrides.calls"
log="$TMP/bootstrap/runtime-overrides.log"
out="$TMP/bootstrap/runtime-overrides.out"
touch "$calls"
rc=0
cat "$ROOT/install.sh" | env -u BASH_ENV PATH="$BOOTSTRAP_BIN" \
  BOOTSTRAP_BIN="$BOOTSTRAP_BIN" MOCK_DOCKER="$TMP/bootstrap/docker-mock" \
  SCENARIO=official CALLS="$calls" DOCKER_MIRROR=test-mirror \
  LINKPILOT_DIR="$runtime_dir" LINKPILOT_LOG="$log" \
  LINKPILOT_DRY_RUN=1 LINKPILOT_PLAIN=1 LINKPILOT_VERBOSE=0 \
  PANEL_PORT=28874 PANEL_BIND=0.0.0.0 PANEL_AGENT_PORT=11176 \
  bash >"$out" 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then
  cat "$out" "$log" >&2
  exit "$rc"
fi
! grep -Eq 'HTTP 直连模式|安全组/NAT映射|内网 IP' "$out"
grep -qx 'PANEL_PORT=28874' "$runtime_dir/.env"
grep -qx 'PANEL_BIND=0.0.0.0' "$runtime_dir/.env"
grep -qx 'PANEL_AGENT_PORT=11176' "$runtime_dir/.env"
grep -qx 'PANEL_AGENT_BIND=127.0.0.1' "$runtime_dir/.env"
grep -qx 'PANEL_ALLOW_INSECURE_AGENT=1' "$runtime_dir/.env"
grep -qx 'PANEL_AGENT_URL=' "$runtime_dir/.env"
printf 'install.sh runtime env overrides OK\n'

printf 'install.sh Docker bootstrap / package fallback / failure reporting OK\n'
