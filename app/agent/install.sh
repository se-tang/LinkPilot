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

cap_available() {
  local bit hex num
  case "$1" in
    CAP_SYS_ADMIN) bit=2097152 ;;
    CAP_NET_RAW) bit=8192 ;;
    *) return 1 ;;
  esac
  hex="$(awk '$1 == "CapEff:" {print $2; exit}' /proc/self/status 2>/dev/null || true)"
  [ -n "$hex" ] || return 1
  num="$(printf '%d' "0x$hex" 2>/dev/null)" || return 1
  [ $((num & bit)) -ne 0 ]
}

container_kind() {
  local kind
  kind="$(systemd-detect-virt 2>/dev/null || true)"
  if [ -z "$kind" ] || [ "$kind" = "none" ]; then
    if [ -e /run/.containerenv ]; then
      kind="podman"
    elif [ -e /.dockerenv ]; then
      kind="docker"
    elif [ -s /run/systemd/container ]; then
      kind="$(tr -d '\r\n' </run/systemd/container)"
    fi
  fi
  printf '%s' "$kind"
}

CONTAINER_KIND="$(container_kind)"
SANDBOX_COMPAT=0
PING_COMPAT=0
case "${LINKPILOT_CONTAINER_COMPAT:-auto}" in
  1|yes|force)
    SANDBOX_COMPAT=1
    PING_COMPAT=1
    ;;
  0|no|off)
    ;;
  *)
    cap_available CAP_NET_RAW || PING_COMPAT=1
    if [ -n "$CONTAINER_KIND" ] && ! cap_available CAP_SYS_ADMIN; then
      SANDBOX_COMPAT=1
    fi
    ;;
esac

HAS_SYSTEMD=0
HAS_OPENRC=0
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  HAS_SYSTEMD=1
elif command -v rc-service >/dev/null 2>&1 \
     && command -v rc-update >/dev/null 2>&1 \
     && [ -x /sbin/openrc-run ]; then
  HAS_OPENRC=1
fi
case "${LINKPILOT_CONTAINER_COMPAT:-auto}" in
  0|no|off) ;;
  *) if [ "$HAS_OPENRC" = "1" ]; then PING_COMPAT=1; fi ;;
esac

HOSTNAME_S="$(hostname 2>/dev/null || echo '-')"
printf '\n  %s%sLinkPilot%s %s节点接入%s\n' "$B" "$MAG" "$R" "$D" "$R"
rule
printf '  %s主机%s   %s\n' "$D" "$R" "$HOSTNAME_S"
printf '  %s面板%s   %s\n' "$D" "$R" "$PANEL_URL"
if [ "$SANDBOX_COMPAT" = "1" ]; then
  COMPAT_MODE="容器兼容模式"
  if [ "$HAS_SYSTEMD" = "1" ]; then COMPAT_MODE="systemd 兼容模式"; fi
  if [ "$HAS_OPENRC" = "1" ]; then COMPAT_MODE="OpenRC 兼容模式"; fi
  printf '  %s兼容%s   %s\n' "$D" "$R" "受限 ${CONTAINER_KIND:-容器}，自动使用 $COMPAT_MODE"
fi
if [ "$PING_COMPAT" = "1" ]; then
  printf '  %s兼容%s   %s\n' "$D" "$R" "缺少 CAP_NET_RAW，自动使用无文件能力 ping"
fi
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
ensure_pkg ping iputils-ping iputils iputils-ping >/dev/null 2>&1 || true
if ! command -v iperf3 >/dev/null 2>&1 || ! command -v ping >/dev/null 2>&1; then
  printf '%s失败%s\n' "$RED" "$R"
  fail "iperf3 或 ping 安装失败，请先手动安装"
  exit 1
fi
printf '%s完成%s\n' "$GRN" "$R"
if [ "$PING_COMPAT" = "0" ] && command -v setcap >/dev/null 2>&1; then
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
# 面板推送 Agent 更新用的根密钥：只写进 /etc/linkpilot/update.key（0600 root），
# Agent 自己读不到，所以即使 Agent 账号被拿下也无法伪造一个“新 Agent”让 root 装上。
UPDATE_KEY="$(printf '%s' "$REG_RESP" | sed -n 's/.*"update_key"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
if ! printf '%s' "$TOKEN" | grep -Eq '^[0-9a-f]{64}$' \
   || ! printf '%s' "$SIGN_KEY" | grep -Eq '^[0-9a-f]{64}$'; then
  printf '%s失败%s\n' "$RED" "$R"
  fail "注册响应格式无效"
  exit 1
fi
if [ -n "$UPDATE_KEY" ] && ! printf '%s' "$UPDATE_KEY" | grep -Eq '^[0-9a-f]{64}$'; then
  printf '%s失败%s\n' "$RED" "$R"
  fail "注册响应里的更新密钥格式无效"
  exit 1
fi
printf '%s完成%s\n' "$GRN" "$R"

if ! getent group linkpilot >/dev/null 2>&1; then
  if command -v groupadd >/dev/null 2>&1; then
    groupadd --system linkpilot
  elif command -v addgroup >/dev/null 2>&1; then
    addgroup -S linkpilot
  else
    fail "无法创建 linkpilot 系统组"
    exit 1
  fi
fi
if ! id linkpilot >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --gid linkpilot --home-dir /var/lib/linkpilot \
      --shell /usr/sbin/nologin linkpilot
  elif command -v adduser >/dev/null 2>&1; then
    adduser -S -D -H -G linkpilot -h /var/lib/linkpilot -s /sbin/nologin linkpilot
  else
    fail "无法创建 linkpilot 系统用户"
    exit 1
  fi
fi

AGENT_FILE_GROUP=linkpilot
CONF_DIR_GROUP=linkpilot
if [ "$HAS_OPENRC" = "1" ] && [ "$PING_COMPAT" = "1" ]; then
  # Alpine's start-stop-daemon can set the effective GID but not arbitrary
  # supplementary groups. The process keeps UID linkpilot and uses GID root
  # for unprivileged ICMP datagrams, so its executable/config must be root-group.
  AGENT_FILE_GROUP=root
  CONF_DIR_GROUP=root
fi

install -d -m 0755 -o root -g root /usr/local/lib/linkpilot
install -d -m 0750 -o root -g "$CONF_DIR_GROUP" /etc/linkpilot
install -d -m 0750 -o linkpilot -g linkpilot /var/lib/linkpilot

if [ "$PING_COMPAT" = "1" ]; then
  dot; printf '容器兼容 ping ... '
  PING_SRC="$(command -v ping)"
  PING_DST=/usr/local/lib/linkpilot/ping
  if [ "$PING_SRC" != "$PING_DST" ]; then
    install -m 0755 "$PING_SRC" "$PING_DST"
  fi
  if command -v setcap >/dev/null 2>&1; then
    setcap -r "$PING_DST" 2>/dev/null || true
  fi
  PING_GROUP_START="$(awk 'NR == 1 {print $1}' /proc/sys/net/ipv4/ping_group_range 2>/dev/null || true)"
  if [ -n "$PING_GROUP_START" ] && [ "$PING_GROUP_START" -gt 0 ] 2>/dev/null; then
    printf '%s失败%s\n' "$RED" "$R"
    fail "容器的 ping_group_range 不接受 root 组，无法启用无特权 ICMP"
    exit 1
  fi
  printf '%s完成%s\n' "$GRN" "$R"
fi

cat > /usr/local/lib/linkpilot/agent.sh << 'AGENT_EOF'
#!/bin/bash
set -eu
umask 077
if [ -x /usr/local/lib/linkpilot/ping ]; then
  PATH="/usr/local/lib/linkpilot:$PATH"
  export PATH
fi
CONF=/etc/linkpilot/agent.conf
SPOOL=/var/lib/linkpilot
AGENT_SELF=/usr/local/lib/linkpilot/agent.sh
CONF_VALUE() { sed -n "s/^$1=//p" "$CONF" | head -n1; }
PANEL_URL="$(CONF_VALUE PANEL_URL)"
TOKEN="$(CONF_VALUE TOKEN)"
SIGN_KEY="$(CONF_VALUE SIGN_KEY)"
[ -n "$PANEL_URL" ] && [ -n "$TOKEN" ] && [ -n "$SIGN_KEY" ] || exit 1

# 自身摘要与能力：面板据此判断「这台机器的 Agent 是不是旧版本、能不能推送更新」。
AGENT_HASH="$( (sha256sum "$AGENT_SELF" 2>/dev/null || shasum -a 256 "$AGENT_SELF" 2>/dev/null) | awk '{print $1}')"
AGENT_CAPS=""
if [ -f /etc/linkpilot/update.key ] && [ -f /usr/local/lib/linkpilot/apply-update.sh ] \
   && { [ -d /run/systemd/system ] || [ -f /etc/init.d/linkpilot-agent-watch ]; }; then
  AGENT_CAPS="update"
fi
# 每次启动记一行：排查「Agent 是哪个版本、能不能被推送更新」时不用猜
echo "$(date '+%F %T') Agent 启动 hash=${AGENT_HASH:0:12} caps=${AGENT_CAPS:-none}" >> "$SPOOL/agent.log"

# 服务 stop/restart（包括推送更新后的重启）时优雅退出，不要卡在 sleep/心跳里被强杀
trap 'exit 0' TERM INT

token_mark="$SPOOL/token_mark"
if [ "$(cat "$token_mark" 2>/dev/null || echo '')" != "$TOKEN" ]; then
  echo 0 > "$SPOOL/last_job_id"
  printf '%s' "$TOKEN" > "$token_mark"
fi

heartbeat() {
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/heartbeat" \
    -H "X-Agent-Token: $TOKEN" \
    -H 'X-Agent-Protocol: 2' \
    -H "X-Agent-Host: $(hostname)" \
    -H "X-Agent-Script: $AGENT_HASH" \
    -H "X-Agent-Caps: $AGENT_CAPS" 2>/dev/null
}

send_output() {
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/output" \
    -H "X-Agent-Token: $TOKEN" \
    --data-urlencode "job_id=$1" \
    --data-urlencode "text@$2" \
    --data-urlencode "done=$3" \
    --data-urlencode "exit_code=$4" >/dev/null 2>&1
}

# 任务执行期间的心跳：告诉面板「我在忙」，面板只回取消指令、不再派新任务。
busy_heartbeat() {
  curl -fsS -m 5 -X POST "$PANEL_URL/api/agent/heartbeat" \
    -H "X-Agent-Token: $TOKEN" \
    -H 'X-Agent-Protocol: 2' \
    -H 'X-Agent-Busy: 1' \
    -H "X-Agent-Host: $(hostname)" \
    -H "X-Agent-Script: $AGENT_HASH" \
    -H "X-Agent-Caps: $AGENT_CAPS" 2>/dev/null
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
      want_json="$(P json)"; omit="$(P omit)"
      valid_ip "$host" && in_range "$port" 1 65535 && in_range "$duration" 1 300 \
        && in_range "$streams" 1 32 || return 2
      args=(iperf3 -c "$host" -p "$port" -t "$duration" -P "$streams")
      if [ "$udp" = "1" ]; then valid_bw "$bw" || return 2; args+=(-u -b "$bw"); fi
      [ "$reverse" = "1" ] && args+=(-R)
      # -O：剔除前 N 秒，避免 TCP 慢启动把短测平均值拉低（UDP 不剔除，保留全部丢包统计）
      case "$omit" in ''|*[!0-9]*) ;; *) [ "$omit" -gt 0 ] && in_range "$omit" 0 30 && args+=(-O "$omit") ;; esac
      # -J：结构化输出，面板可直接算每个 interval 的稳定性；旧面板不带该参数，解析时回退文本
      [ "$want_json" = "1" ] && args+=(-J)
      "${args[@]}"
      ;;
    ping)
      host="$(P host)"; count="$(P count)"
      valid_ip "$host" && in_range "$count" 1 2000 || return 2
      ping -c "$count" -i 1 "$host"
      ;;
    update_agent)
      sha="$(P sha256)"; ts="$(P ts)"; sig_u="$(P sig)"
      [ -n "$sha" ] && [ ${#sha} -eq 64 ] || return 2
      printf '%s' "$sha" | grep -Eq '^[0-9a-f]{64}$' || return 2
      valid_int "$ts" || return 2
      [ -n "$sig_u" ] || return 2
      [ "$AGENT_CAPS" = "update" ] || {
        echo "本机 Agent 不具备推送更新能力（缺少 apply-update / update.key），请重跑接入命令"
        return 3
      }
      staged="$SPOOL/update.staged"
      curl -fsSL -m 60 "$PANEL_URL/agent/agent.sh" -o "$staged" || {
        echo "下载新 Agent 失败"; rm -f "$staged"; return 1
      }
      got="$( (sha256sum "$staged" 2>/dev/null || shasum -a 256 "$staged" 2>/dev/null) | awk '{print $1}')"
      if [ "$got" != "$sha" ]; then
        echo "校验失败：下载内容 $got 与面板声明的 $sha 不一致"
        rm -f "$staged"; return 1
      fi
      bash -n "$staged" || { echo "新 Agent 语法检查失败"; rm -f "$staged"; return 1; }
      printf '%s\n' "$sha" > "$SPOOL/update.sha"
      printf '%s\n' "$ts" > "$SPOOL/update.ts"
      printf '%s\n' "$sig_u" > "$SPOOL/update.sig"
      echo "UPDATE_STAGED sha=$sha（root 助手将在本任务回报后替换并重启 Agent）"
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
    ensure_tools|report_ips|check_daemon|start_server|stop_server|check_port|iperf_client|ping|update_agent|uninstall) ;;
    *)
      # 未知任务类型也要回报一次：否则面板上的任务会永远停在 running，
      # 「推送更新」这类新任务在旧 Agent 上就表现为「一直没反应」。
      printf '拒绝未知任务类型：%s\n' "$kind" >> "$SPOOL/agent.log"
      printf '拒绝未知任务类型：%s（该 Agent 版本过旧，请重跑接入命令）\n' "$kind" > "$SPOOL/reject"
      send_output "$job_id" "$SPOOL/reject" 1 2
      sleep 3; continue ;;
  esac

  out="$SPOOL/job.out"; : > "$out"
  run_job >"$out" 2>&1 & pid=$!
  offset=0; start=$(date +%s); tick=0
  while kill -0 "$pid" 2>/dev/null; do
    size=$(wc -c <"$out" 2>/dev/null || echo 0)
    if [ "$size" -gt "$offset" ]; then
      tail -c +"$((offset + 1))" "$out" >"$SPOOL/chunk"
      offset=$size
      send_output "$job_id" "$SPOOL/chunk" 0 ""
    fi
    # 执行期间每 3 秒发一次忙碌心跳：维持在线状态，同时收面板的取消指令。
    # 没有这一路心跳，面板「停止测试」只能在本地面上置标志位，远端的 iperf3
    # 会继续跑满时长，既占着目标机端口又会和下一轮抢带宽。
    tick=$((tick + 1))
    if [ $((tick % 3)) -eq 1 ]; then
      hb="$(busy_heartbeat)" || hb=""
      case "$hb" in
        *"cancel_job=$job_id"*)
          echo "$(date '+%F %T') 面板要求中止任务 #$job_id" >> "$SPOOL/agent.log"
          pkill -TERM -P "$pid" 2>/dev/null || true
          kill -TERM "$pid" 2>/dev/null || true
          ;;
      esac
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

  # 新 Agent 已就位：等本次任务回报完，再触发 root 助手替换并重启（重启会结束本进程）。
  if [ -f "$SPOOL/update.sha" ] && [ -f "$SPOOL/update.staged" ]; then
    echo "$(date '+%F %T') 触发 Agent 自更新（sha=$(cat "$SPOOL/update.sha" 2>/dev/null)）" >> "$SPOOL/agent.log"
    : >"$SPOOL/update.request"
    sleep 10
  fi
done
AGENT_EOF
chown "root:${AGENT_FILE_GROUP}" /usr/local/lib/linkpilot/agent.sh
chmod 0750 /usr/local/lib/linkpilot/agent.sh

cat > /etc/linkpilot/agent.conf << CONF_EOF
PANEL_URL=$PANEL_URL
TOKEN=$TOKEN
SIGN_KEY=$SIGN_KEY
PROTOCOL=2
CONF_EOF
chown "root:${CONF_DIR_GROUP}" /etc/linkpilot/agent.conf
chmod 0640 /etc/linkpilot/agent.conf
printf '%s\n' "$AGENT_FILE_GROUP" >/etc/linkpilot/agent.group
chown root:root /etc/linkpilot/agent.group
chmod 0644 /etc/linkpilot/agent.group

# 推送更新用的根密钥：只有 root 可读，Agent 账号读不到（防「Agent 被拿下 → 借 root 助手装自己的脚本」）
if [ -n "$UPDATE_KEY" ]; then
  printf '%s\n' "$UPDATE_KEY" > /etc/linkpilot/update.key
  chown root:root /etc/linkpilot/update.key
  chmod 0600 /etc/linkpilot/update.key
fi

cat > /usr/local/lib/linkpilot/uninstall.sh << 'UNINSTALL_EOF'
#!/bin/bash
set -u
# 先停掉所有相关单元：agent 本体、卸载触发、更新触发（后两个是 path 单元，
# 不 disable 的话删了文件它们还会留在 systemd 里一直等触发条件）
systemctl disable --now linkpilot-agent >/dev/null 2>&1 || true
systemctl disable --now linkpilot-agent-uninstall.path >/dev/null 2>&1 || true
systemctl disable --now linkpilot-agent-update.path >/dev/null 2>&1 || true
if command -v rc-service >/dev/null 2>&1; then
  rc-service linkpilot-agent-watch stop >/dev/null 2>&1 || true
  rc-service linkpilot-agent stop >/dev/null 2>&1 || true
fi
if command -v rc-update >/dev/null 2>&1; then
  rc-update del linkpilot-agent-watch default >/dev/null 2>&1 || true
  rc-update del linkpilot-agent default >/dev/null 2>&1 || true
fi
rm -f /etc/systemd/system/linkpilot-agent.service \
      /etc/systemd/system/linkpilot-agent-uninstall.service \
      /etc/systemd/system/linkpilot-agent-uninstall.path \
      /etc/systemd/system/linkpilot-agent-update.service \
      /etc/systemd/system/linkpilot-agent-update.path
rm -f /etc/init.d/linkpilot-agent /etc/init.d/linkpilot-agent-watch
rm -f /run/linkpilot-agent.pid /run/linkpilot-agent-watch.pid
rm -f /etc/systemd/system/linkpilot-agent.service.d/container-compat.conf
rmdir /etc/systemd/system/linkpilot-agent.service.d 2>/dev/null || true
systemctl daemon-reload >/dev/null 2>&1 || true
systemctl reset-failed >/dev/null 2>&1 || true
crontab -u linkpilot -r >/dev/null 2>&1 || true
pkill -f /usr/local/lib/linkpilot/agent.sh >/dev/null 2>&1 || true
rm -rf /var/lib/linkpilot /etc/linkpilot /usr/local/lib/linkpilot
rm -f /etc/sudoers.d/linkpilot-uninstall
userdel linkpilot >/dev/null 2>&1 || true
UNINSTALL_EOF
chmod 0750 /usr/local/lib/linkpilot/uninstall.sh

# 推送更新：Agent（低权限）只负责把新脚本落到 /var/lib/linkpilot 并核对 sha256，
# 真正的替换由 root 助手完成，且助手要用 /etc/linkpilot/update.key 验签。
if [ -n "$UPDATE_KEY" ]; then
  cat > /usr/local/lib/linkpilot/apply-update.sh << 'UPDATE_EOF'
#!/bin/bash
# 由 root 执行的 Agent 更新助手：验签 -> 语法检查 -> 原子替换 -> 重启 Agent。
# systemd 由 path unit 触发，OpenRC 由 linkpilot-agent-watch 轮询触发。
set -u
SPOOL=/var/lib/linkpilot
KEY_FILE=/etc/linkpilot/update.key
TARGET=/usr/local/lib/linkpilot/agent.sh
LOG="$SPOOL/update.log"

say() { echo "$(date '+%F %T') $*" >> "$LOG"; }
cleanup() { rm -f "$SPOOL/update.request" "$SPOOL/update.staged" "$SPOOL/update.sha" \
                  "$SPOOL/update.ts" "$SPOOL/update.sig"; }
fail() { say "更新失败：$*"; cleanup; exit 1; }

[ -f "$SPOOL/update.request" ] || exit 0
[ -r "$KEY_FILE" ] || fail "缺少 $KEY_FILE"
[ -s "$SPOOL/update.staged" ] || fail "缺少暂存脚本"
sha=$(tr -d '\r\n' < "$SPOOL/update.sha" 2>/dev/null)
ts=$(tr -d '\r\n' < "$SPOOL/update.ts" 2>/dev/null)
sig=$(tr -d '\r\n' < "$SPOOL/update.sig" 2>/dev/null)
case "$sha" in *[!0-9a-f]*|'') fail "sha256 非法" ;; esac
[ ${#sha} -eq 64 ] || fail "sha256 长度不对"
case "$ts" in *[!0-9]*|'') fail "时间戳非法" ;; esac
now=$(date +%s)
[ $((now - ts)) -le 3600 ] || fail "签名已过期"
[ $((ts - now)) -le 300 ] || fail "时间戳在未来"

got=$(sha256sum "$SPOOL/update.staged" | awk '{print $1}')
[ "$got" = "$sha" ] || fail "文件摘要不匹配"
expect=$(printf '%s' "update:$sha:$ts" | openssl dgst -sha256 -hmac "$(cat "$KEY_FILE")" | awk '{print $NF}')
[ -n "$expect" ] && [ "$expect" = "$sig" ] || fail "签名校验不通过"
bash -n "$SPOOL/update.staged" || fail "脚本语法检查失败"

group=$(cat /etc/linkpilot/agent.group 2>/dev/null || echo linkpilot)
install -m 0750 -o root -g "$group" "$SPOOL/update.staged" "$TARGET" || fail "写入 $TARGET 失败"
say "已替换 Agent（sha=$sha），重启服务"
cleanup
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  systemctl restart linkpilot-agent 2>/dev/null || true
elif command -v rc-service >/dev/null 2>&1 && [ -x /etc/init.d/linkpilot-agent ]; then
  rc-service linkpilot-agent restart >/dev/null 2>&1 || true
fi
exit 0
UPDATE_EOF
  chmod 0750 /usr/local/lib/linkpilot/apply-update.sh
  chown root:root /usr/local/lib/linkpilot/apply-update.sh
fi

AGENT_MODE=""
dot; printf '注册常驻服务 ... '
if [ "$HAS_SYSTEMD" = "1" ]; then
  LEGACY_COMPAT_DROPIN=/etc/systemd/system/linkpilot-agent.service.d/container-compat.conf
  if [ -f "$LEGACY_COMPAT_DROPIN" ] \
     && grep -q 'The host is a restricted Podman container' "$LEGACY_COMPAT_DROPIN" 2>/dev/null; then
    rm -f "$LEGACY_COMPAT_DROPIN"
    rmdir /etc/systemd/system/linkpilot-agent.service.d 2>/dev/null || true
  fi
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
UNIT_EOF
  if [ "$SANDBOX_COMPAT" = "1" ]; then
    cat >> /etc/systemd/system/linkpilot-agent.service << 'UNIT_EOF'
# Restricted container: mount/user-namespace hardening needs CAP_SYS_ADMIN.
UNIT_EOF
  else
    cat >> /etc/systemd/system/linkpilot-agent.service << 'UNIT_EOF'
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ReadWritePaths=/var/lib/linkpilot
UNIT_EOF
  fi
  if [ "$PING_COMPAT" = "1" ]; then
    cat >> /etc/systemd/system/linkpilot-agent.service << 'UNIT_EOF'
SupplementaryGroups=root
Environment=PATH=/usr/local/lib/linkpilot:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
UNIT_EOF
  else
    cat >> /etc/systemd/system/linkpilot-agent.service << 'UNIT_EOF'
CapabilityBoundingSet=CAP_NET_RAW CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_RAW CAP_NET_BIND_SERVICE
UNIT_EOF
  fi
  cat >> /etc/systemd/system/linkpilot-agent.service << 'UNIT_EOF'

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
  if [ -n "$UPDATE_KEY" ]; then
    cat > /etc/systemd/system/linkpilot-agent-update.service << 'UNIT_EOF'
[Unit]
Description=LinkPilot Agent update apply

[Service]
Type=oneshot
ExecStart=/usr/local/lib/linkpilot/apply-update.sh
UNIT_EOF
    cat > /etc/systemd/system/linkpilot-agent-update.path << 'UNIT_EOF'
[Unit]
Description=Watch LinkPilot Agent update request

[Path]
PathExists=/var/lib/linkpilot/update.request
Unit=linkpilot-agent-update.service

[Install]
WantedBy=multi-user.target
UNIT_EOF
    systemctl daemon-reload
    systemctl enable --now linkpilot-agent-update.path >/dev/null 2>&1 || true
  fi
  systemctl restart linkpilot-agent || true
  if systemctl is-active --quiet linkpilot-agent; then
    AGENT_MODE="systemd"
    printf '%s完成%s %s(低权限用户 linkpilot)%s\n' "$GRN" "$R" "$D" "$R"
  else
    printf '%s失败%s\n' "$RED" "$R"
    note 'journalctl -u linkpilot-agent -n 30'
    exit 1
  fi
elif [ "$HAS_OPENRC" = "1" ]; then
  OPENRC_AGENT_GROUP=root
  : >/var/lib/linkpilot/agent.log
  chown linkpilot:linkpilot /var/lib/linkpilot/agent.log
  chmod 0600 /var/lib/linkpilot/agent.log
  cat > /etc/init.d/linkpilot-agent << 'OPENRC_EOF'
#!/sbin/openrc-run

name="LinkPilot Agent"
description="LinkPilot low-privilege node agent"
pidfile="/run/linkpilot-agent.pid"
logfile="/var/lib/linkpilot/agent.log"

start() {
  ebegin "Starting ${name}"
  export RC_SVCNAME
  supervise-daemon "$RC_SVCNAME" --start \
    --user linkpilot --group __AGENT_GROUP__ --no-new-privs \
    --pidfile "$pidfile" \
    --stdout "$logfile" --stderr "$logfile" \
    --respawn-delay 5 \
    -- /bin/bash /usr/local/lib/linkpilot/agent.sh
  eend $?
}

stop() {
  ebegin "Stopping ${name}"
  supervise-daemon "$RC_SVCNAME" --stop \
    --pidfile "$pidfile" --retry TERM/10/KILL/5
  eend $?
}

status() {
  ebegin "Checking ${name}"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    eend 0
  else
    eend 1
  fi
}

depend() {
  need net
  after firewall
}
OPENRC_EOF
  sed -i "s/__AGENT_GROUP__/$OPENRC_AGENT_GROUP/g" /etc/init.d/linkpilot-agent
  chmod 0755 /etc/init.d/linkpilot-agent

  if [ -n "$UPDATE_KEY" ]; then
    cat > /usr/local/lib/linkpilot/watch-root-requests.sh << 'WATCH_EOF'
#!/bin/sh
SPOOL=/var/lib/linkpilot
while :; do
  if [ -f "$SPOOL/uninstall.request" ]; then
    rm -f "$SPOOL/uninstall.request"
    setsid /usr/local/lib/linkpilot/uninstall.sh \
      >>"$SPOOL/uninstall.log" 2>&1 &
    exit 0
  fi
  if [ -f "$SPOOL/update.request" ]; then
    /usr/local/lib/linkpilot/apply-update.sh || true
  fi
  sleep 2
done
WATCH_EOF
    chmod 0750 /usr/local/lib/linkpilot/watch-root-requests.sh
    chown root:root /usr/local/lib/linkpilot/watch-root-requests.sh
    cat > /etc/init.d/linkpilot-agent-watch << 'OPENRC_EOF'
#!/sbin/openrc-run

name="LinkPilot Agent root request watcher"
description="Applies signed LinkPilot Agent updates"
pidfile="/run/linkpilot-agent-watch.pid"
logfile="/var/lib/linkpilot/root-watch.log"

start() {
  ebegin "Starting ${name}"
  export RC_SVCNAME
  supervise-daemon "$RC_SVCNAME" --start \
    --pidfile "$pidfile" \
    --stdout "$logfile" --stderr "$logfile" \
    --respawn-delay 5 \
    -- /bin/sh /usr/local/lib/linkpilot/watch-root-requests.sh
  eend $?
}

stop() {
  ebegin "Stopping ${name}"
  supervise-daemon "$RC_SVCNAME" --stop \
    --pidfile "$pidfile" --retry TERM/10/KILL/5
  eend $?
}

status() {
  ebegin "Checking ${name}"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    eend 0
  else
    eend 1
  fi
}

depend() {
  need net
  after linkpilot-agent
}
OPENRC_EOF
    chmod 0755 /etc/init.d/linkpilot-agent-watch
  fi

  rc-update add linkpilot-agent default >/dev/null 2>&1
  if [ -x /etc/init.d/linkpilot-agent-watch ]; then
    rc-update add linkpilot-agent-watch default >/dev/null 2>&1
    rc-service linkpilot-agent-watch restart >/dev/null 2>&1 || true
  fi
  rc-service linkpilot-agent restart || true
  if rc-service linkpilot-agent status >/dev/null 2>&1; then
    AGENT_MODE="openrc"
    printf '%s完成%s %s(OpenRC，低权限用户 linkpilot)%s\n' "$GRN" "$R" "$D" "$R"
  else
    printf '%s失败%s\n' "$RED" "$R"
    note 'rc-service linkpilot-agent status; tail -n 30 /var/lib/linkpilot/agent.log'
    exit 1
  fi
else
  pkill -f /usr/local/lib/linkpilot/agent.sh >/dev/null 2>&1 || true
  if [ "$PING_COMPAT" = "1" ] \
     && ! id -nG linkpilot 2>/dev/null | tr ' ' '\n' | grep -qx 'root'; then
    if command -v usermod >/dev/null 2>&1; then
      usermod -aG root linkpilot
    elif command -v gpasswd >/dev/null 2>&1; then
      gpasswd -a linkpilot root >/dev/null
    else
      fail "无 systemd 且无法把 linkpilot 加入 root 组以启用无特权 ICMP"
      exit 1
    fi
  fi
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
