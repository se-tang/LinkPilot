"""派发给 Agent 执行的 shell 任务脚本（在目标机器上运行，要求 root）。"""
import ipaddress
import re


def valid_port(port, default=5201):
    """把用户填的端口收敛成 1–65535 的整数，非法值退回默认端口。"""
    try:
        p = int(str(port).strip())
    except (TypeError, ValueError):
        return default
    return p if 1 <= p <= 65535 else default


# 检查并安装 iperf3 / ping（apt → dnf → yum+epel → apk → zypper 依次尝试）
SCRIPT_ENSURE = r'''
export DEBIAN_FRONTEND=noninteractive
have() { command -v "$1" >/dev/null 2>&1; }
if ! have ping; then
  echo "[setup] ping 未找到，尝试安装 iputils..."
  apt-get install -y iputils-ping 2>/dev/null \
    || yum install -y iputils 2>/dev/null \
    || dnf install -y iputils 2>/dev/null \
    || apk add --no-cache iputils-ping 2>/dev/null \
    || echo "[setup] ping 安装失败（可能影响 ping 测试）"
fi
if have iperf3; then
  echo "[setup] iperf3 已安装: $(iperf3 --version 2>&1 | head -n1)"
  exit 0
fi
echo "[setup] 未检测到 iperf3，开始自动安装..."
ok=0
if have apt-get; then
  apt-get update -y >/dev/null 2>&1
  apt-get install -y iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have dnf; then
  dnf install -y iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have yum; then
  yum install -y epel-release >/dev/null 2>&1
  yum install -y iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have apk; then
  apk add --no-cache iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have zypper; then
  # openSUSE 的包名是 iperf（包里的二进制才叫 iperf3，Provides 里没有 iperf3），
  # 所以先按 iperf3 试，装不上再回退到 iperf，否则这个分支在 openSUSE 上永远失败
  zypper --non-interactive install iperf3 >/dev/null 2>&1 \
    || zypper --non-interactive install iperf >/dev/null 2>&1
  have iperf3 && ok=1
fi
if have iperf3; then
  echo "[setup] iperf3 安装成功: $(iperf3 --version 2>&1 | head -n1)"
else
  echo "[setup] iperf3 自动安装失败，请手动安装后重试"
  exit 1
fi
'''.strip()

# iperf3 server 的存活上限（秒）：正常收尾由 script_stop_server 立即关闭，
# 这个 TTL 只是「面板崩溃 / 任务中断导致关闭指令送不到」时的兜底自过期，
# 所以按本轮测试的预估串行时长给（见 runner.server_ttl），短测试不再白留半小时。
SERVER_TTL_DEFAULT = 1800
SERVER_TTL_MIN = 300
SERVER_TTL_MAX = 3600


def clamp_ttl(seconds, default=SERVER_TTL_DEFAULT):
    """把 server 存活秒数收敛到 [SERVER_TTL_MIN, SERVER_TTL_MAX]；非法值退回默认值。"""
    try:
        t = int(float(seconds))
    except (TypeError, ValueError):
        return default
    return max(SERVER_TTL_MIN, min(SERVER_TTL_MAX, t))


# 每轮测试开始：清掉同端口的旧进程后启动 iperf3 -s（也用于通道内掉线自愈重启）
# 每台后端机可以用各自的端口，所以 pid / 日志按端口分开，清扫也只清同端口，
# 避免「起第二个端口的 server 时把第一个踢掉」。
# timeout $TTL：server 到点自过期，防止面板崩溃/任务中断后残留进程被扫描器滥用
# （过期后若有后续测试，通道内端口预检失败会自动重启 server）
# 端口由用户自定义（默认 5201），模板里用 __PORT__ / __TTL__ 占位后替换，避免 format 转义问题
SCRIPT_START_SERVER_TMPL = r'''
PORT=__PORT__
TTL=__TTL__
PIDF="/tmp/iperf3-server-$PORT.pid"
LOG="/tmp/iperf3-server-$PORT.log"
[ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null
command -v pkill >/dev/null 2>&1 && pkill -f "iperf3 -s -p $PORT" 2>/dev/null
sleep 0.5
nohup timeout "$TTL" iperf3 -s -p "$PORT" > "$LOG" 2>&1 &
echo $! > "$PIDF"
sleep 1
if kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "SERVER_STARTED port=$PORT ttl=$TTL"
else
  echo "SERVER_FAILED port=$PORT"
  cat "$LOG" 2>/dev/null
  exit 1
fi
'''.strip()


def script_start_server(port=5201, ttl=SERVER_TTL_DEFAULT):
    """启动目标机指定端口上的 iperf3 server（TCP/UDP 共用同一个 -s 进程）。

    多台后端机可以各用各的端口，因此这里只影响该端口对应的进程。
    ttl 是该 server 的存活上限（秒）：正常收尾面板会立即下发 script_stop_server
    关掉它，TTL 只兜底面板崩溃/关闭指令丢失的情况。
    """
    return (SCRIPT_START_SERVER_TMPL
            .replace('__PORT__', str(valid_port(port)))
            .replace('__TTL__', str(clamp_ttl(ttl))))


SCRIPT_STOP_SERVER_TMPL = r'''
PORTS="__PORTS__"
# 先按 pid 文件精确回收：这些 pid 文件只会由本面板的启动脚本创建，清掉它们不会
# 影响机器上与本面板无关的 iperf3；再对本次真正用过的端口做一次兜底匹配。
# 不再使用不带端口的裸 pkill 模式（会误杀用户自己或别的工具起的 iperf3 server）。
for f in /tmp/iperf3-server*.pid; do
  [ -e "$f" ] || continue
  kill "$(cat "$f" 2>/dev/null)" 2>/dev/null
  rm -f "$f"
done
for p in $PORTS; do
  command -v pkill >/dev/null 2>&1 && pkill -f "iperf3 -s -p $p" 2>/dev/null
done
echo "IPERF3_SERVER_STOPPED ports=$PORTS"
'''.strip()


def script_stop_server(ports=()):
    """关闭目标机上由本面板启动的 iperf3 server。

    ports 传本轮真正起过的端口（见 runner._worker 的 started_ports）：pid 文件清扫
    覆盖全部端口，逐端口匹配只针对这些端口，从而避免误杀无关的 iperf3 进程。
    """
    ps = []
    for p in ports or ():
        v = valid_port(p, None)
        if v is not None and v not in ps:
            ps.append(v)
    return SCRIPT_STOP_SERVER_TMPL.replace(
        '__PORTS__', ' '.join(str(p) for p in sorted(ps)))


# 开测前的体检：找出发行版自带的 iperf3 常驻服务、以及本次要用的端口是否已被别人监听。
# 背景：Debian / Ubuntu 的 iperf3 包在装的时候会问「是否作为守护进程启动」（默认 No），
# 但只要有人答过 Yes，这台机器就会有一个 systemd 常驻服务一直监听 5201：它的命令行是
# `iperf3 --server --interval 0`（不带 -p），而且 Restart=always —— 面板既杀不掉它，
# 也别想在同端口起自己的 server。所以这里提前把情况问清楚，好给出能直接照做的提示。
SCRIPT_CHECK_DAEMON_TMPL = r'''
PORTS="__PORTS__"
have() { command -v "$1" >/dev/null 2>&1; }

state=absent
if have systemctl; then
  if systemctl is-active --quiet iperf3 2>/dev/null; then
    state=active
  elif systemctl is-enabled --quiet iperf3 2>/dev/null; then
    state=enabled            # 当前没跑，但开机自启，重启后会占住端口
  else
    state=inactive
  fi
fi
echo "DISTRO_DAEMON=$state"

# 本次要用的端口是否已有人在监听；有本面板 pid 文件的说明是我们自己上一轮残留的，
# 启动脚本会先把它换掉，所以不算冲突，跳过以免误报。
for p in $PORTS; do
  [ -f "/tmp/iperf3-server-$p.pid" ] && continue
  line=""
  if have ss; then
    line=$(ss -ltnp 2>/dev/null | grep -E "[:.]$p[[:space:]]" | head -n1)
  elif have netstat; then
    line=$(netstat -ltnp 2>/dev/null | grep -E "[:.]$p[[:space:]]" | head -n1)
  fi
  [ -n "$line" ] && echo "PORT_BUSY=$p $line"
done
echo "DAEMON_CHECK_DONE"
'''.strip()


def script_check_daemon(ports=()):
    """生成开测前体检脚本：发行版 iperf3 常驻服务状态 + 本次端口是否被占用。"""
    ps = []
    for p in ports or ():
        v = valid_port(p, None)
        if v is not None and v not in ps:
            ps.append(v)
    return SCRIPT_CHECK_DAEMON_TMPL.replace(
        '__PORTS__', ' '.join(str(p) for p in sorted(ps)))


def script_check_port(ip, port=5201):
    """探测目标机 iperf3 端口是否可达。IPv4 优先用 bash /dev/tcp（瞬时完成），不可用时回退 curl；
    IPv6 直接用 curl（bash 的 /dev/tcp 不支持 IPv6 字面量，地址需带方括号）。
    连接被拒=7 / 连接超时=28 视为不通，其余视为可达（iperf3 不是 HTTP，能连上就算通）。"""
    port = valid_port(port)
    host = f'[{ip}]' if ipaddress.ip_address(ip).version == 6 else ip
    return (
        'host="{host}"; port="{port}"\n'
        'probe() {{\n'
        '  if [ {ipv6} != "1" ] && command -v bash >/dev/null 2>&1; then\n'
        '    if timeout 4 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then return 0; fi\n'
        '  fi\n'
        '  if command -v curl >/dev/null 2>&1; then\n'
        '    curl -s -o /dev/null --connect-timeout 3 -m 4 "http://$host:$port/" >/dev/null 2>&1\n'
        '    rc=$?\n'
        '    [ "$rc" != "7" ] && [ "$rc" != "28" ] && return 0\n'
        '  fi\n'
        '  return 1\n'
        '}}\n'
        'if probe; then echo PORT_OK; else echo PORT_BLOCKED; exit 1; fi'
    ).format(host=host, port=port, ipv6='1' if ipaddress.ip_address(ip).version == 6 else '0')


# 探测本机可用于测试的全局 IPv4 / IPv6 地址。
# 只做本地查询（ip route get 是路由查表，不发送任何数据包），不访问第三方网站；
# 输出 IPV4= / IPV6=（缺失则留空），面板解析后写入机器资料。
# 用途：面板只监听 IPv4 时也能拿到机器的 IPv6 地址，避免必须支持双栈面板。
SCRIPT_REPORT_IPS = r'''
r4=""
r6=""
if command -v ip >/dev/null 2>&1; then
  r4=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9][0-9.]*\).*/\1/p' | head -n1)
  # 全局 IPv6：排除已弃用地址、ULA(fc/fd) 与链路本地(fe80)。
  # 优先取稳定地址（开启隐私扩展的机器会额外生成 temporary 地址）；
  # 若该机只有临时地址，退而使用临时地址——否则这类机器会完全探测不到 IPv6。
  cands=$(ip -6 addr show scope global 2>/dev/null | awk '
    /inet6/ && $0 !~ /deprecated/ {
      a = $2; sub(/\/.*/, "", a);
      if (a ~ /^(f[cd]|fe80)/) next;
      print ($0 ~ /temporary/ ? "tmp" : "stable"), a;
    }')
  r6=$(printf '%s\n' "$cands" | awk '$1 == "stable" {print $2; exit}')
  [ -z "$r6" ] && r6=$(printf '%s\n' "$cands" | awk 'NF {print $2; exit}')
fi
[ -z "$r4" ] && r4=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' | head -n1)
[ -z "$r6" ] && r6=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep ':' | grep -v -iE '^(fe80|f[cd])' | head -n1)
echo "IPV4=${r4}"
echo "IPV6=${r6}"
'''.strip()


def parse_ip_report(out):
    """解析 SCRIPT_REPORT_IPS 的输出，只接受「协议族与字段匹配 + 全局可路由」的地址。
    返回如 {'ip4': '1.2.3.4'} / {'ip6': '2408:...'}，无有效地址时为空字典。"""
    found = {}
    for line in (out or '').splitlines():
        m = re.match(r'^(IPV4|IPV6)=(.*)$', line.strip())
        if not m:
            continue
        key = 'ip4' if m.group(1) == 'IPV4' else 'ip6'
        try:
            addr = ipaddress.ip_address(m.group(2).strip())
        except ValueError:
            continue
        if addr.version != (4 if key == 'ip4' else 6) or not addr.is_global:
            continue
        found[key] = addr.compressed
    return found
