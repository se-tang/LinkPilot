#!/bin/bash
# linkpilot agent 一键安装脚本（由面板下发）
# 用法: curl -fsSL http://面板地址/agent/install.sh | bash -s -- http://面板地址 接入令牌 签名密钥
#       （机器上只有 wget 时：wget -qO- http://面板地址/agent/install.sh | bash -s -- http://面板地址 接入令牌 签名密钥）
set -e

PANEL_URL="${1:-}"
TOKEN="${2:-}"
SIGN_KEY="${3:-}"

# ---------------------------------------------------------------------------
# 输出风格：与面板部署脚本一致（安静 + 进度 + 结论）
#   管道 / CI 下自动退化为纯文本；NO_COLOR=1 可强制关闭颜色
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && command -v tput >/dev/null 2>&1 \
   && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ] 2>/dev/null; then
  R="$(tput sgr0)"; B="$(tput bold)"; D="$(tput dim)"
  GRN="$(tput setaf 2)"; YEL="$(tput setaf 3)"; RED="$(tput setaf 1)"
  CYN="$(tput setaf 6)"; MAG="$(tput setaf 5)"
else
  R=""; B=""; D=""; GRN=""; YEL=""; RED=""; CYN=""; MAG=""
fi
ok()   { printf '  %s✓%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf '  %s!%s %s\n' "$YEL$B" "$R" "$*"; }
fail() { printf '  %s✗%s %s\n' "$RED$B" "$R" "$*" >&2; }
note() { printf '      %s%s%s\n' "$D" "$*" "$R"; }
dot()  { printf '  %s▸%s ' "$CYN" "$R"; }
rule() { printf '%s%s%s\n' "$D" "──────────────────────────────────────────────────────────────" "$R"; }

usage() {
  printf '\n'
  printf '  %s%sLinkPilot%s %s节点接入%s\n' "$B" "$MAG" "$R" "$D" "$R"
  rule
  printf '  用法: %scurl -fsSL <面板地址>/agent/install.sh | bash -s -- <面板地址> <接入令牌> <签名密钥>%s\n' "$D" "$R"
  note '只有 wget 的机器：wget -qO- <面板地址>/agent/install.sh | bash -s -- <面板地址> <接入令牌> <签名密钥>'
  printf '  %s接入命令请到面板「机器管理 → 接入命令」复制（每个令牌唯一）%s\n' "$D" "$R"
  printf '\n'
}

if [ -z "$PANEL_URL" ] || [ -z "$TOKEN" ] || [ -z "$SIGN_KEY" ]; then
  if [ -n "$PANEL_URL" ] && [ -n "$TOKEN" ] && [ -z "$SIGN_KEY" ]; then
    fail "检测到旧版接入命令（缺少第 3 个参数「签名密钥」）"
    note '请回到面板 → 机器管理 → 「接入命令」，重新复制最新的接入命令后执行。'
  else
    usage
  fi
  exit 1
fi
case "$PANEL_URL" in http://*|https://*) ;; *) PANEL_URL="http://$PANEL_URL" ;; esac
HOSTNAME_S="$(hostname 2>/dev/null || echo '-')"

printf '\n'
printf '  %s%sLinkPilot%s %s节点接入%s\n' "$B" "$MAG" "$R" "$D" "$R"
rule
printf '  %s主机%s   %s\n' "$D" "$R" "$HOSTNAME_S"
printf '  %s面板%s   %s\n' "$D" "$R" "$PANEL_URL"
rule
printf '\n'

if [ "$(id -u)" != "0" ]; then
  fail "请使用 root 用户执行（或 sudo bash）"
  exit 1
fi

pkg_mgr() {
  for _c in apt-get dnf yum zypper apk; do
    command -v "$_c" >/dev/null 2>&1 && { printf '%s' "$_c"; return 0; }
  done
  printf ''
}

ensure_pkg() { # $1=命令 $2=包名(apt/dnf/yum/zypper) $3=包名(alpine) $4=可选的 rpm 包名覆盖
  command -v "$1" >/dev/null 2>&1 && return 0
  _pm="$(pkg_mgr)"
  case "$_pm" in
    apt-get)
      # 全新机器 apt 包列表可能是空的，必须先 update，否则 install 一定失败
      DEBIAN_FRONTEND=noninteractive apt-get update -qq 2>/dev/null || true
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$2" 2>/dev/null || true
      ;;
    dnf) dnf install -y "${4:-$2}" 2>/dev/null || true ;;
    yum) yum install -y "${4:-$2}" 2>/dev/null || true ;;
    zypper) zypper --non-interactive install "${4:-$2}" 2>/dev/null || true ;;
    apk) apk add --no-cache "$3" 2>/dev/null || true ;;
    *)
      apt-get install -y "$2" 2>/dev/null || yum install -y "$2" 2>/dev/null \
        || dnf install -y "$2" 2>/dev/null || apk add --no-cache "$3" 2>/dev/null || true
      ;;
  esac
  command -v "$1" >/dev/null 2>&1
}

# ---- 依赖：curl / openssl（Agent 用 openssl 校验任务签名） ----
for _tool in curl openssl; do
  if command -v "$_tool" >/dev/null 2>&1; then
    printf '  %s▸%s %s ... %s已安装%s\n' "$CYN" "$R" "$_tool" "$GRN" "$R"
    continue
  fi
  dot; printf '%s ... ' "$_tool"
  if ensure_pkg "$_tool" "$_tool" "$_tool"; then
    printf '%s已安装%s\n' "$GRN" "$R"
  else
    printf '%s安装失败%s\n' "$RED$B" "$R"
    if [ "$_tool" = "curl" ]; then
      note 'Debian/Ubuntu: apt-get update && apt-get install -y curl'
      note 'CentOS/RHEL/Rocky: yum install -y curl      Alpine: apk add curl'
    else
      note 'Debian/Ubuntu: apt-get install -y openssl   CentOS/RHEL: yum install -y openssl'
    fi
    fail "缺少 $_tool，无法继续"
    exit 1
  fi
done

mkdir -p /usr/local/lib/linkpilot /etc/linkpilot /var/lib/linkpilot

cat > /usr/local/lib/linkpilot/agent.sh << 'AGENT_EOF'
#!/bin/bash
# linkpilot agent 主循环：心跳领取任务 → 验签 → 执行 → 流式回传输出
CONF=/etc/linkpilot/agent.conf
[ -f "$CONF" ] || exit 1
. "$CONF"
SPOOL=/var/lib/linkpilot
mkdir -p "$SPOOL" 2>/dev/null

# 重新接入（令牌变了）后必须清掉任务编号水位线：面板侧该机器的记录已重建，
# 签名密钥随之更换，历史任务的签名必然校验失败，清掉水位不会引入重放风险。
# 不清的话，万一面板数据库被重置（任务编号重新从 1 开始），Agent 会把之后所有
# 新任务都当成「重放」静默拒绝——表现就是机器在线，但地址探测不出来、任务全部超时。
token_mark="$SPOOL/token_mark"
if [ "$(cat "$token_mark" 2>/dev/null || echo '')" != "$TOKEN" ]; then
  echo 0 > "$SPOOL/last_job_id"
  printf '%s' "$TOKEN" > "$token_mark"
  chmod 600 "$token_mark" 2>/dev/null
fi

heartbeat() {
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/heartbeat" \
    -H "X-Agent-Token: $TOKEN" -H "X-Agent-Host: $(hostname)" 2>/dev/null
}

send_output() { # $1=job_id $2=file $3=done $4=exit_code
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/output" \
    -H "X-Agent-Token: $TOKEN" \
    --data-urlencode "job_id=$1" \
    --data-urlencode "text@$2" \
    --data-urlencode "done=$3" \
    --data-urlencode "exit_code=$4" >/dev/null 2>&1
}

# 任务签名校验：签名密钥只在接入时下发一次，任何没有签名的任务一律拒绝执行
verify_sig() { # $1=job_id $2=cmd_b64 $3=sig
  [ -n "$SIGN_KEY" ] || return 1
  [ -n "$3" ] || return 1
  calc=$(printf '%s' "$1:$2" | openssl dgst -sha256 -hmac "$SIGN_KEY" 2>/dev/null | awk '{print $NF}')
  [ -n "$calc" ] && [ "$calc" = "$3" ]
}

while true; do
  resp=$(heartbeat)
  hb_rc=$?
  if [ $hb_rc -ne 0 ]; then
    echo "$(date '+%F %T') 心跳失败 curl_exit=$hb_rc（7=连接被拒 28=超时 22=令牌被面板拒绝）" >> "$SPOOL/agent.log"
    sleep 5
    continue
  fi
  job_id=$(printf '%s\n' "$resp" | sed -n 's/^job_id=//p')
  if [ -z "$job_id" ]; then
    sleep 3
    continue
  fi
  timeout=$(printf '%s\n' "$resp" | sed -n 's/^timeout=//p')
  timeout=${timeout:-120}
  sig=$(printf '%s\n' "$resp" | sed -n 's/^sig=//p')
  cmd_b64=$(printf '%s\n' "$resp" | sed -n 's/^cmd_b64=//p')

  if ! verify_sig "$job_id" "$cmd_b64" "$sig"; then
    echo "$(date '+%F %T') 拒绝执行：任务 #$job_id 签名校验失败（疑似被篡改）" >> "$SPOOL/agent.log"
    sleep 5
    continue
  fi

  # 防重放：数值任务编号必须递增（墓碑等特殊任务除外）
  case "$job_id" in
    ''|*[!0-9]*) ;;
    *) last=$(cat "$SPOOL/last_job_id" 2>/dev/null || echo 0)
       if [ "$job_id" -le "$last" ]; then
         echo "$(date '+%F %T') 拒绝执行：任务 #$job_id 为重放" >> "$SPOOL/agent.log"
         sleep 5
         continue
       fi
       echo "$job_id" > "$SPOOL/last_job_id" ;;
  esac

  printf '%s\n' "$resp" | sed -n 's/^cmd_b64=//p' | base64 -d > "$SPOOL/job.sh" 2>/dev/null

  out="$SPOOL/job.out"
  : > "$out"
  bash "$SPOOL/job.sh" > "$out" 2>&1 &
  pid=$!
  offset=0
  start=$(date +%s)

  while kill -0 "$pid" 2>/dev/null; do
    size=$(wc -c < "$out" 2>/dev/null || echo 0)
    if [ "$size" -gt "$offset" ]; then
      tail -c +"$((offset + 1))" "$out" > "$SPOOL/chunk" 2>/dev/null
      offset=$size
      send_output "$job_id" "$SPOOL/chunk" 0 ""
    fi
    now=$(date +%s)
    if [ $((now - start)) -gt $((timeout + 120)) ]; then
      kill "$pid" 2>/dev/null
    fi
    sleep 1
  done

  wait "$pid"
  exit_code=$?
  size=$(wc -c < "$out" 2>/dev/null || echo 0)
  if [ "$size" -gt "$offset" ]; then
    tail -c +"$((offset + 1))" "$out" > "$SPOOL/chunk" 2>/dev/null
    send_output "$job_id" "$SPOOL/chunk" 0 ""
  fi
  : > "$SPOOL/done"
  send_output "$job_id" "$SPOOL/done" 1 "$exit_code"
done
AGENT_EOF
chmod 700 /usr/local/lib/linkpilot/agent.sh
printf '  %s▸%s 写入 Agent 程序 ... %s完成%s\n' "$CYN" "$R" "$GRN" "$R"

cat > /etc/linkpilot/agent.conf << CONF_EOF
PANEL_URL='$PANEL_URL'
TOKEN='$TOKEN'
SIGN_KEY='$SIGN_KEY'
CONF_EOF
chmod 600 /etc/linkpilot/agent.conf
printf '  %s▸%s 写入接入凭据 ... %s完成%s %s(仅 root 可读)%s\n' "$CYN" "$R" "$GRN" "$R" "$D" "$R"

# ---- 常驻方式：systemd 优先，无 systemd 时退回 nohup + cron @reboot ----
AGENT_MODE=""
dot; printf '注册为常驻服务 ... '
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  cat > /etc/systemd/system/linkpilot-agent.service << 'UNIT_EOF'
[Unit]
Description=linkpilot agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/bin/bash /usr/local/lib/linkpilot/agent.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT_EOF
  systemctl daemon-reload
  systemctl enable linkpilot-agent >/dev/null 2>&1
  # 必须用 restart：重复安装时 enable --now 不会重启已运行的服务，
  # 会导致新令牌/签名密钥不生效、Agent 一直离线
  systemctl restart linkpilot-agent
  if systemctl is-active --quiet linkpilot-agent; then
    AGENT_MODE="systemd"
    printf '%s完成%s %s(systemd: linkpilot-agent)%s\n' "$GRN" "$R" "$D" "$R"
  else
    printf '%s失败%s\n' "$RED$B" "$R"
    note '查看原因：journalctl -u linkpilot-agent -n 20'
    fail "Agent 未能启动"
    exit 1
  fi
else
  pkill -f "linkpilot/agent.sh" 2>/dev/null || true
  (setsid nohup bash /usr/local/lib/linkpilot/agent.sh >> /var/lib/linkpilot/agent.log 2>&1 &)
  (crontab -l 2>/dev/null | grep -v "linkpilot/agent.sh"
   echo "@reboot bash /usr/local/lib/linkpilot/agent.sh >> /var/lib/linkpilot/agent.log 2>&1") | crontab - 2>/dev/null || true
  AGENT_MODE="nohup"
  printf '%s完成%s %s(未检测到 systemd：nohup + cron @reboot)%s\n' "$GRN" "$R" "$D" "$R"
fi

# ---- 连通性自检：能访问面板才算接入成功 ----
dot; printf '检查到面板的连通性 ... '
if curl -fsS -m 5 "$PANEL_URL/api/health" >/dev/null 2>&1; then
  printf '%s正常%s\n' "$GRN" "$R"
else
  printf '%s不通%s\n' "$RED$B" "$R"
  printf '\n'
  rule
  fail "本机访问不到面板 $PANEL_URL"
  note 'Agent 无法上线。请检查：'
  note '  1) 面板机的防火墙 / 云安全组是否放行了该端口（含出方向的回包）'
  note '  2) 面板地址是否可从公网访问（NAT / 反代场景要用对外那个地址）'
  note "  3) 放行后重试：systemctl restart linkpilot-agent"
  rule
  exit 1
fi

printf '  %s▸%s 等待面板确认接入 ... ' "$CYN" "$R"
sleep 3
printf '%s完成%s\n' "$GRN" "$R"

printf '\n'
rule
printf '  %s%s✓ 接入完成%s\n' "$B" "$GRN" "$R"
printf '  %s主机%s   %s\n' "$D" "$R" "$HOSTNAME_S"
printf '  %s面板%s   %s\n' "$D" "$R" "$PANEL_URL"
printf '  %s守护%s   %s\n' "$D" "$R" "$([ "$AGENT_MODE" = "systemd" ] && echo 'systemd（开机自启，异常自动重启）' || echo 'nohup + cron @reboot')"
printf '\n'
printf '  %s下一步%s 回到面板刷新，机器应显示「在线」；随后即可把它选进测试或定时任务。\n' "$B" "$R"
printf '  %s排障%s   机器本地日志 %s/var/lib/linkpilot/agent.log%s\n' "$B" "$R" "$D" "$R"
rule
printf '\n'
