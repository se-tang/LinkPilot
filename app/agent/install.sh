#!/bin/bash
# LinkPilot Agent installer.
# Usage: curl -fsSL https://panel/agent/install.sh | bash -s -- https://panel CODE
set -eu
umask 077

PANEL_URL="${1:-}"
REG_CODE="${2:-}"
LEGACY_SECRET="${3:-}"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && command -v tput >/dev/null 2>&1; then
  R="$(tput sgr0)"; B="$(tput bold)"; D="$(tput dim)"
  GRN="$(tput setaf 2)"; YEL="$(tput setaf 3)"; RED="$(tput setaf 1)"
  CYN="$(tput setaf 6)"; MAG="$(tput setaf 5)"
else
  R=""; B=""; D=""; GRN=""; YEL=""; RED=""; CYN=""; MAG=""
fi
ok() { printf '  %s✓%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf '  %s!%s %s\n' "$YEL$B" "$R" "$*"; }
fail() { printf '  %s✗%s %s\n' "$RED$B" "$R" "$*" >&2; }
note() { printf '      %s%s%s\n' "$D" "$*" "$R"; }
dot() { printf '  %s▸%s ' "$CYN" "$R"; }
rule() { printf '%s%s%s\n' "$D" "──────────────────────────────────────────────────────────────" "$R"; }

usage() {
  printf '\n  %s%sLinkPilot%s %s节点接入%s\n' "$B" "$MAG" "$R" "$D" "$R"
  rule
  printf '  用法: %scurl -fsSL <HTTPS 面板地址>/agent/install.sh | bash -s -- <面板地址> <一次性接入码>%s\n' "$D" "$R"
  note '接入码到面板「机器管理 → 接入」重新生成，10 分钟内有效且只能使用一次。'
  printf '\n'
}

if [ -n "$LEGACY_SECRET" ]; then
  fail "检测到旧版接入命令（长期 Token / SignKey 不再接受）"
  note '请回到面板重新生成一次性接入码，再复制新命令。'
  exit 1
fi
if [ -z "$PANEL_URL" ] || [ -z "$REG_CODE" ]; then
  usage
  exit 1
fi
if ! printf '%s' "$REG_CODE" | grep -Eq '^[A-Z0-9]{10}$'; then
  fail "一次性接入码格式无效"
  exit 1
fi
case "$PANEL_URL" in
  http://*|https://*) ;;
  *) PANEL_URL="https://$PANEL_URL" ;;
esac
case "$PANEL_URL" in
  *[[:space:]]*|*"'"*|*'"'*|*'`'*|*'$'*|*'|'*|*';'*|*'&'*)
    fail "面板地址格式无效"
    exit 1
    ;;
esac
case "$PANEL_URL" in
  http://?*|https://?*) ;;
  *)
  fail "面板地址格式无效"
  exit 1
  ;;
esac
PANEL_URL="${PANEL_URL%/}"

if [ "$(id -u)" != "0" ]; then
  fail "请使用 root 用户执行安装脚本（仅安装阶段需要 root）"
  exit 1
fi

HOSTNAME_S="$(hostname 2>/dev/null || echo '-')"
printf '\n  %s%sLinkPilot%s %s节点接入%s\n' "$B" "$MAG" "$R" "$D" "$R"
rule
printf '  %s主机%s   %s\n' "$D" "$R" "$HOSTNAME_S"
printf '  %s面板%s   %s\n' "$D" "$R" "$PANEL_URL"
rule
printf '\n'

pkg_mgr() {
  for c in apt-get dnf yum zypper apk; do
    command -v "$c" >/dev/null 2>&1 && { printf '%s' "$c"; return 0; }
  done
  printf ''
}

ensure_pkg() {
  command -v "$1" >/dev/null 2>&1 && return 0
  pm="$(pkg_mgr)"
  case "$pm" in
    apt-get)
      DEBIAN_FRONTEND=noninteractive apt-get update -qq 2>/dev/null || true
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$2" 2>/dev/null || true
      ;;
    dnf) dnf install -y "${3:-$2}" 2>/dev/null || true ;;
    yum) yum install -y "${3:-$2}" 2>/dev/null || true ;;
    zypper) zypper --non-interactive install "${3:-$2}" 2>/dev/null || true ;;
    apk) apk add --no-cache "${4:-$2}" 2>/dev/null || true ;;
  esac
  command -v "$1" >/dev/null 2>&1
}

for tool in curl openssl; do
  dot; printf '依赖 %s ... ' "$tool"
  if ensure_pkg "$tool" "$tool"; then printf '%s完成%s\n' "$GRN" "$R"; else
    printf '%s失败%s\n' "$RED" "$R"; fail "缺少 $tool"; exit 1
  fi
done

dot; printf 'iperf3 / ping ... '
ensure_pkg iperf3 iperf3 iperf3 iperf3 >/dev/null 2>&1 || true
if ! command -v iperf3 >/dev/null 2>&1 && [ "$(pkg_mgr)" = "zypper" ]; then
  zypper --non-interactive install iperf >/dev/null 2>&1 || true
fi
ensure_pkg ping iputils-ping iputils iputils >/dev/null 2>&1 || true
if ! command -v iperf3 >/dev/null 2>&1 || ! command -v ping >/dev/null 2>&1; then
  printf '%s失败%s\n' "$RED" "$R"
  fail "iperf3 或 ping 安装失败，请先手动安装"
  exit 1
fi
printf '%s完成%s\n' "$GRN" "$R"
if command -v setcap >/dev/null 2>&1; then
  setcap cap_net_raw+ep "$(command -v ping)" 2>/dev/null || true
fi

if [ "${PANEL_URL#http://}" != "$PANEL_URL" ] && [ "${LINKPILOT_INSECURE_HTTP:-0}" != "1" ]; then
  fail "拒绝通过明文 HTTP 注册 Agent"
  note "请使用 HTTPS 地址；仅限本地实验时可显式设置 LINKPILOT_INSECURE_HTTP=1。"
  exit 1
fi

dot; printf '一次性接入码换领凭据 ... '
REG_BODY="$(printf '{"code":"%s"}' "$REG_CODE")"
if ! REG_RESP="$(curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/register" \
    -H 'Content-Type: application/json' --data "$REG_BODY" 2>/dev/null)"; then
  printf '%s失败%s\n' "$RED" "$R"
  fail "接入码无效、已过期，或面板未启用 HTTPS"
  exit 1
fi
TOKEN="$(printf '%s' "$REG_RESP" | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
SIGN_KEY="$(printf '%s' "$REG_RESP" | sed -n 's/.*"sign_key"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
if ! printf '%s' "$TOKEN" | grep -Eq '^[0-9a-f]{64}$' \
   || ! printf '%s' "$SIGN_KEY" | grep -Eq '^[0-9a-f]{64}$'; then
  printf '%s失败%s\n' "$RED" "$R"
  fail "注册响应格式无效"
  exit 1
fi
printf '%s完成%s\n' "$GRN" "$R"

if ! id linkpilot >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/linkpilot --shell /usr/sbin/nologin linkpilot
  elif command -v adduser >/dev/null 2>&1; then
    adduser -S -D -H -h /var/lib/linkpilot -s /sbin/nologin linkpilot
  else
    fail "无法创建 linkpilot 系统用户"
    exit 1
  fi
fi

install -d -m 0755 -o root -g root /usr/local/lib/linkpilot
install -d -m 0750 -o root -g linkpilot /etc/linkpilot
install -d -m 0750 -o linkpilot -g linkpilot /var/lib/linkpilot

cat > /usr/local/lib/linkpilot/agent.sh << 'AGENT_EOF'
#!/bin/bash
set -eu
umask 077
CONF=/etc/linkpilot/agent.conf
SPOOL=/var/lib/linkpilot
CONF_VALUE() { sed -n "s/^$1=//p" "$CONF" | head -n1; }
PANEL_URL="$(CONF_VALUE PANEL_URL)"
TOKEN="$(CONF_VALUE TOKEN)"
SIGN_KEY="$(CONF_VALUE SIGN_KEY)"
[ -n "$PANEL_URL" ] && [ -n "$TOKEN" ] && [ -n "$SIGN_KEY" ] || exit 1

token_mark="$SPOOL/token_mark"
if [ "$(cat "$token_mark" 2>/dev/null || echo '')" != "$TOKEN" ]; then
  echo 0 > "$SPOOL/last_job_id"
  printf '%s' "$TOKEN" > "$token_mark"
fi

heartbeat() {
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/heartbeat" \
    -H "X-Agent-Token: $TOKEN" \
    -H 'X-Agent-Protocol: 2' \
    -H "X-Agent-Host: $(hostname)" 2>/dev/null
}

send_output() {
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/output" \
    -H "X-Agent-Token: $TOKEN" \
    --data-urlencode "job_id=$1" \
    --data-urlencode "text@$2" \
    --data-urlencode "done=$3" \
    --data-urlencode "exit_code=$4" >/dev/null 2>&1
}

verify_sig() {
  [ -n "$SIGN_KEY" ] && [ -n "$3" ] || return 1
  calc=$(printf '%s' "$1:$2" | openssl dgst -sha256 -hmac "$SIGN_KEY" 2>/dev/null | awk '{print $NF}')
  [ -n "$calc" ] && printf '%s' "$calc" | grep -Fqx "$3"
}

P() { sed -n "s/^$1=//p" "$SPOOL/payload" | head -n1; }
valid_int() { case "$1" in ''|*[!0-9]*) return 1;; esac; }
in_range() { valid_int "$1" && [ "$1" -ge "$2" ] && [ "$1" -le "$3" ]; }
valid_ip() { printf '%s' "$1" | grep -Eq '^[0-9A-Fa-f:.]+$' && { printf '%s' "$1" | grep -q ':' || printf '%s' "$1" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; }; }
valid_bw() { printf '%s' "$1" | grep -Eq '^[0-9]{1,5}(\.[0-9]{1,3})?[KMG]$'; }

local_bind() {
  want="$1"
  if command -v ip >/dev/null 2>&1 && ip -o addr show 2>/dev/null | grep -Fq " $want/"; then
    printf '%s' "$want"; return 0
  fi
  if command -v ip >/dev/null 2>&1; then
    case "$want" in
      *:*) ip -6 route get 2606:4700::1111 2>/dev/null ;;
      *) ip -4 route get 1.1.1.1 2>/dev/null ;;
    esac | sed -n 's/.* src \([^ ]*\).*/\1/p' | head -n1
  else
    for addr in $(hostname -I 2>/dev/null); do
      case "$want" in
        *:*) case "$addr" in *:*) printf '%s' "$addr"; return 0 ;; esac ;;
        *) case "$addr" in *:*) ;; *) printf '%s' "$addr"; return 0 ;; esac ;;
      esac
    done
  fi
}

report_ips() {
  r4=""; r6=""
  if command -v ip >/dev/null 2>&1; then
    r4=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9][0-9.]*\).*/\1/p' | head -n1)
    cands=$(ip -6 addr show scope global 2>/dev/null | awk '
      /inet6/ && $0 !~ /deprecated/ {
        a=$2; sub(/\/.*/,"",a);
        if (a ~ /^(f[cd]|fe80)/) next;
        print ($0 ~ /temporary/ ? "tmp" : "stable"), a;
      }')
    r6=$(printf '%s\n' "$cands" | awk '$1=="stable"{print $2;exit}')
    [ -n "$r6" ] || r6=$(printf '%s\n' "$cands" | awk 'NF{print $2;exit}')
  fi
  [ -n "$r4" ] || r4=$(hostname -I 2>/dev/null | tr ' ' '\n' \
    | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' | head -n1)
  [ -n "$r6" ] || r6=$(hostname -I 2>/dev/null | tr ' ' '\n' \
    | grep ':' | grep -viE '^(fe80|f[cd])' | head -n1)
  echo "IPV4=$r4"
  echo "IPV6=$r6"
}

run_job() {
  kind="$(P kind)"
  case "$kind" in
    ensure_tools)
      command -v iperf3 >/dev/null && command -v ping >/dev/null
      ;;
    report_ips)
      report_ips
      ;;
    check_daemon)
      ports="$(P ports)"
      state=absent
      if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet iperf3 2>/dev/null; then state=active
        elif systemctl is-enabled --quiet iperf3 2>/dev/null; then state=enabled
        else state=inactive; fi
      fi
      echo "DISTRO_DAEMON=$state"
      IFS=',' read -ra pp <<< "$ports"
      for p in "${pp[@]}"; do
        valid_int "$p" || continue
        [ -f "/tmp/iperf3-server-$p.pid" ] && continue
        line=""
        if command -v ss >/dev/null 2>&1; then
          line=$(ss -ltnp 2>/dev/null | grep -E "[:.]$p[[:space:]]" | head -n1)
        elif command -v netstat >/dev/null 2>&1; then
          line=$(netstat -ltnp 2>/dev/null | grep -E "[:.]$p[[:space:]]" | head -n1)
        fi
        [ -z "$line" ] || echo "PORT_BUSY=$p $line"
      done
      echo DAEMON_CHECK_DONE
      ;;
    start_server)
      port="$(P port)"; ttl="$(P ttl)"; bind="$(P bind)"
      in_range "$port" 1 65535 && in_range "$ttl" 300 3600 && valid_ip "$bind" || return 2
      chosen="$(local_bind "$bind")"
      [ -n "$chosen" ] || { echo "BIND_FAILED $bind"; return 1; }
      pidf="/tmp/iperf3-server-$port.pid"; log="/tmp/iperf3-server-$port.log"
      [ -f "$pidf" ] && kill "$(cat "$pidf")" 2>/dev/null || true
      sleep 0.3
      nohup timeout "$ttl" iperf3 -s -B "$chosen" -p "$port" >"$log" 2>&1 &
      echo $! >"$pidf"
      sleep 1
      if kill -0 "$(cat "$pidf")" 2>/dev/null; then
        echo "SERVER_STARTED port=$port ttl=$ttl bind=$chosen"
      else
        echo "SERVER_FAILED port=$port bind=$chosen"; cat "$log" 2>/dev/null; return 1
      fi
      ;;
    stop_server)
      ports="$(P ports)"
      IFS=',' read -ra pp <<< "$ports"
      for p in "${pp[@]}"; do
        valid_int "$p" || continue
        pidf="/tmp/iperf3-server-$p.pid"
        [ -f "$pidf" ] && kill "$(cat "$pidf")" 2>/dev/null || true
        rm -f "$pidf"
        pgrep -f "^iperf3 -s -B .* -p $p$" 2>/dev/null | xargs -r kill 2>/dev/null || true
      done
      echo "IPERF3_SERVER_STOPPED ports=$ports"
      ;;
    check_port)
      host="$(P host)"; port="$(P port)"
      valid_ip "$host" && in_range "$port" 1 65535 || return 2
      if printf '%s' "$host" | grep -q ':'; then
        curl -s -o /dev/null --connect-timeout 3 -m 4 "http://[$host]:$port/" >/dev/null 2>&1
      else
        timeout 4 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null
      fi
      rc=$?
      [ "$rc" = "0" ] && { echo PORT_OK; return 0; }
      echo PORT_BLOCKED; return 1
      ;;
    iperf_client)
      host="$(P host)"; port="$(P port)"; duration="$(P duration)"
      streams="$(P streams)"; udp="$(P udp)"; bw="$(P udp_bandwidth)"; reverse="$(P reverse)"
      valid_ip "$host" && in_range "$port" 1 65535 && in_range "$duration" 1 300 \
        && in_range "$streams" 1 32 || return 2
      args=(iperf3 -c "$host" -p "$port" -t "$duration" -P "$streams")
      if [ "$udp" = "1" ]; then valid_bw "$bw" || return 2; args+=(-u -b "$bw"); fi
      [ "$reverse" = "1" ] && args+=(-R)
      "${args[@]}"
      ;;
    ping)
      host="$(P host)"; count="$(P count)"
      valid_ip "$host" && in_range "$count" 1 2000 || return 2
      ping -c "$count" -i 1 "$host"
      ;;
    uninstall)
      touch "$SPOOL/uninstall.request"
      echo "AGENT_UNINSTALL_SCHEDULED"
      ;;
    *)
      echo "拒绝未知任务类型：$kind" >&2
      return 2
      ;;
  esac
}

while true; do
  resp=$(heartbeat); rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "$(date '+%F %T') 心跳失败 curl_exit=$rc" >> "$SPOOL/agent.log"
    sleep 5; continue
  fi
  if printf '%s\n' "$resp" | grep -qx 'status=upgrade_required'; then
    echo "$(date '+%F %T') 面板要求重新接入 Agent" >> "$SPOOL/agent.log"
    sleep 10; continue
  fi
  job_id=$(printf '%s\n' "$resp" | sed -n 's/^job_id=//p')
  [ -n "$job_id" ] || { sleep 3; continue; }
  timeout=$(printf '%s\n' "$resp" | sed -n 's/^timeout=//p'); timeout=${timeout:-120}
  sig=$(printf '%s\n' "$resp" | sed -n 's/^sig=//p')
  payload_b64=$(printf '%s\n' "$resp" | sed -n 's/^payload_b64=//p')
  if ! verify_sig "$job_id" "$payload_b64" "$sig"; then
    echo "$(date '+%F %T') 拒绝任务 #$job_id：签名失败" >> "$SPOOL/agent.log"
    sleep 5; continue
  fi
  case "$job_id" in
    *[!0-9]*) ;;
    *) last=$(cat "$SPOOL/last_job_id" 2>/dev/null || echo 0)
       if [ "$job_id" -le "$last" ]; then
         echo "$(date '+%F %T') 拒绝任务 #$job_id：重放" >> "$SPOOL/agent.log"
         sleep 5; continue
       fi
       echo "$job_id" > "$SPOOL/last_job_id" ;;
  esac
  printf '%s' "$payload_b64" | base64 -d > "$SPOOL/payload" 2>/dev/null
  kind=$(sed -n 's/^kind=//p' "$SPOOL/payload" | head -n1)
  case "$kind" in
    ensure_tools|report_ips|check_daemon|start_server|stop_server|check_port|iperf_client|ping|uninstall) ;;
    *) echo "拒绝未知任务类型：$kind" >> "$SPOOL/agent.log"; sleep 3; continue ;;
  esac

  out="$SPOOL/job.out"; : > "$out"
  run_job >"$out" 2>&1 & pid=$!
  offset=0; start=$(date +%s)
  while kill -0 "$pid" 2>/dev/null; do
    size=$(wc -c <"$out" 2>/dev/null || echo 0)
    if [ "$size" -gt "$offset" ]; then
      tail -c +"$((offset + 1))" "$out" >"$SPOOL/chunk"
      offset=$size
      send_output "$job_id" "$SPOOL/chunk" 0 ""
    fi
    now=$(date +%s)
    [ $((now - start)) -le $((timeout + 120)) ] || kill "$pid" 2>/dev/null || true
    sleep 1
  done
  exit_code=0
  wait "$pid" || exit_code=$?
  size=$(wc -c <"$out" 2>/dev/null || echo 0)
  if [ "$size" -gt "$offset" ]; then
    tail -c +"$((offset + 1))" "$out" >"$SPOOL/chunk"
    send_output "$job_id" "$SPOOL/chunk" 0 ""
  fi
  : >"$SPOOL/done"
  send_output "$job_id" "$SPOOL/done" 1 "$exit_code"
done
AGENT_EOF
chown root:linkpilot /usr/local/lib/linkpilot/agent.sh
chmod 0750 /usr/local/lib/linkpilot/agent.sh

cat > /etc/linkpilot/agent.conf << CONF_EOF
PANEL_URL=$PANEL_URL
TOKEN=$TOKEN
SIGN_KEY=$SIGN_KEY
PROTOCOL=2
CONF_EOF
chown root:linkpilot /etc/linkpilot/agent.conf
chmod 0640 /etc/linkpilot/agent.conf

cat > /usr/local/lib/linkpilot/uninstall.sh << 'UNINSTALL_EOF'
#!/bin/bash
set -u
systemctl disable --now linkpilot-agent >/dev/null 2>&1 || true
rm -f /etc/systemd/system/linkpilot-agent.service
systemctl daemon-reload >/dev/null 2>&1 || true
crontab -u linkpilot -r >/dev/null 2>&1 || true
pkill -f /usr/local/lib/linkpilot/agent.sh >/dev/null 2>&1 || true
rm -rf /var/lib/linkpilot /etc/linkpilot /usr/local/lib/linkpilot
rm -f /etc/sudoers.d/linkpilot-uninstall
userdel linkpilot >/dev/null 2>&1 || true
UNINSTALL_EOF
chmod 0750 /usr/local/lib/linkpilot/uninstall.sh

AGENT_MODE=""
dot; printf '注册常驻服务 ... '
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  cat > /etc/systemd/system/linkpilot-agent.service << 'UNIT_EOF'
[Unit]
Description=LinkPilot Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=linkpilot
Group=linkpilot
ExecStart=/bin/bash /usr/local/lib/linkpilot/agent.sh
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ReadWritePaths=/var/lib/linkpilot
CapabilityBoundingSet=CAP_NET_RAW CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_RAW CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT_EOF
  cat > /etc/systemd/system/linkpilot-agent-uninstall.service << 'UNIT_EOF'
[Unit]
Description=LinkPilot Agent cleanup

[Service]
Type=oneshot
ExecStart=/usr/local/lib/linkpilot/uninstall.sh
UNIT_EOF
  cat > /etc/systemd/system/linkpilot-agent-uninstall.path << 'UNIT_EOF'
[Unit]
Description=Watch LinkPilot Agent uninstall request

[Path]
PathExists=/var/lib/linkpilot/uninstall.request
Unit=linkpilot-agent-uninstall.service

[Install]
WantedBy=multi-user.target
UNIT_EOF
  systemctl daemon-reload
  systemctl enable linkpilot-agent >/dev/null 2>&1
  systemctl enable --now linkpilot-agent-uninstall.path >/dev/null 2>&1 || true
  systemctl restart linkpilot-agent || true
  if systemctl is-active --quiet linkpilot-agent; then
    AGENT_MODE="systemd"
    printf '%s完成%s %s(低权限用户 linkpilot)%s\n' "$GRN" "$R" "$D" "$R"
  else
    printf '%s失败%s\n' "$RED" "$R"
    note 'journalctl -u linkpilot-agent -n 30'
    exit 1
  fi
else
  pkill -f /usr/local/lib/linkpilot/agent.sh >/dev/null 2>&1 || true
  if command -v runuser >/dev/null 2>&1; then
    (setsid runuser -u linkpilot -- /bin/sh -c \
      'exec /bin/bash /usr/local/lib/linkpilot/agent.sh >> /var/lib/linkpilot/agent.log 2>&1' &)
  elif command -v su >/dev/null 2>&1; then
    (setsid su -s /bin/sh linkpilot -c \
      'exec /bin/bash /usr/local/lib/linkpilot/agent.sh >> /var/lib/linkpilot/agent.log 2>&1' &)
  else
    fail "系统既无 systemd 也无法以低权限用户启动 Agent"
    exit 1
  fi
  if command -v crontab >/dev/null 2>&1; then
    (crontab -u linkpilot -l 2>/dev/null | grep -v "linkpilot/agent.sh"
     echo "@reboot /bin/bash /usr/local/lib/linkpilot/agent.sh >> /var/lib/linkpilot/agent.log 2>&1") \
      | crontab -u linkpilot - 2>/dev/null || true
  fi
  AGENT_MODE="nohup"
  printf '%s完成%s %s(nohup，低权限用户)%s\n' "$GRN" "$R" "$D" "$R"
fi

dot; printf '连通性自检 ... '
if curl -fsS -m 5 "$PANEL_URL/api/health" >/dev/null 2>&1; then
  printf '%s正常%s\n' "$GRN" "$R"
else
  printf '%s不通%s\n' "$RED" "$R"
  fail "本机访问不到面板 $PANEL_URL"
  exit 1
fi

printf '\n'; rule
printf '  %s✓ Agent 接入完成%s\n' "$GRN$B" "$R"
printf '  %s主机%s   %s\n' "$D" "$R" "$HOSTNAME_S"
printf '  %s面板%s   %s\n' "$D" "$R" "$PANEL_URL"
printf '  %s守护%s   %s\n' "$D" "$R" "$AGENT_MODE"
rule
printf '\n'
