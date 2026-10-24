"""测试编排：通过 Agent 任务队列远程执行（面板不再直连机器）。

流水线：iperf3 上/下行全局串行（多台同时测速会互相抢带宽，结果作废），
ping 不占测速通道，与后续机器的 iperf3 并行执行，充分利用等待时间。

测试参数每次可自定义（见 normalize_params / build_iperf_cmd）：
线程数 -P、单向时长 -t、iperf3 端口 -p、TCP/UDP（-u -b）、ping 次数 -c。

协议：默认 IPv4（两端都用 IPv4 地址，避免机器缺 IPv6 时失败）；
用户确认双端都支持 IPv6 时可选择 IPv6 测试。地址取自机器资料
（心跳观测 + 面板探测，见 db.machine_test_ip）。
"""
import ipaddress
import json
import re
import shlex
import threading
import time
import traceback

from . import agent_jobs as aj
from . import db, job_types, quality

# 测试参数默认值与上限（前端只做提示，这里才是准入门槛）
DEFAULT_PARAMS = {'streams': 1, 'duration': 10, 'port': 5201,
                  'udp': False, 'udp_bandwidth': '100M', 'ping_count': 200}
# UDP 目标带宽：必须带单位（-b 100 在 iperf3 里是 100 bit/s，太容易填错）
_UDP_BW_RE = re.compile(r'^\d{1,5}(?:\.\d{1,3})?[KMG]$')

_state_lock = threading.Lock()
_active = {}  # run_id -> {'stop': bool, 'log': [..]}
_EVENT_PREFIX = '@@EVENT@@'


def _int_in(raw, lo, hi, label, default=None):
    """把值收敛成 [lo, hi] 内的整数；空值返回 default，非法值报错。"""
    if raw is None or str(raw).strip() == '':
        return default
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        raise RuntimeError(f'{label}必须是整数（收到 {raw!r}）')
    if not lo <= v <= hi:
        raise RuntimeError(f'{label}需在 {lo}–{hi} 之间（收到 {v}）')
    return v


def normalize_params(data):
    """校验并收敛前端传来的测试参数；非法值直接报错，不静默改动用户输入。"""
    data = data or {}

    def num(key, lo, hi, label):
        return _int_in(data.get(key), lo, hi, label, DEFAULT_PARAMS[key])

    bw = str(data.get('udp_bandwidth') or DEFAULT_PARAMS['udp_bandwidth']).strip().upper()
    if not _UDP_BW_RE.match(bw):
        raise RuntimeError('UDP 目标带宽必须带单位（示例：100M、1G、500K）')
    # 每台后端机可以单独指定连接端口（{机器ID: 端口}），没指定的跟目标机端口一致
    ports = {}
    raw_ports = data.get('ports')
    if isinstance(raw_ports, dict):
        for k, v in raw_ports.items():
            try:
                mid = int(str(k).strip())
            except (TypeError, ValueError):
                raise RuntimeError(f'机器编号必须是整数（收到 {k!r}）')
            p = _int_in(v, 1, 65535, f'机器 #{mid} 的连接端口')
            if p is not None:
                ports[mid] = p
    return {
        'streams': num('streams', 1, 32, '线程数'),
        'duration': num('duration', 1, 300, '单向测试时长（秒）'),
        'port': num('port', 1, 65535, '目标机端口'),
        'ping_count': num('ping_count', 1, 2000, 'ping 次数'),
        'udp': bool(data.get('udp')),
        'udp_bandwidth': bw,
        'ports': ports,
    }


def run_cfg(run):
    """从测试记录里取出参数（旧记录缺列时退回默认值）。"""
    bw = str((run or {}).get('udp_bandwidth') or DEFAULT_PARAMS['udp_bandwidth']).strip().upper()
    return {
        'streams': int(run.get('streams') or DEFAULT_PARAMS['streams']),
        'duration': int(run.get('duration') or DEFAULT_PARAMS['duration']),
        'port': aj.valid_port(run.get('port') or DEFAULT_PARAMS['port']),
        'ping_count': int(run.get('ping_count') or DEFAULT_PARAMS['ping_count']),
        'udp': bool(int(run.get('udp') or 0)),
        'udp_bandwidth': bw if _UDP_BW_RE.match(bw) else DEFAULT_PARAMS['udp_bandwidth'],
    }


def bidirectional_failure(metrics):
    """上下行必须都有有效吞吐，任一方向缺失时整台机器本轮按失败处理。"""
    up = (metrics or {}).get('up_mbits')
    down = (metrics or {}).get('down_mbits')
    try:
        invalid_up = up is None or float(up) <= 0
        invalid_down = down is None or float(down) <= 0
    except (TypeError, ValueError):
        return '上下行测试失败：吞吐结果包含无效数值'
    if invalid_up:
        return '上行测试失败：未获得有效吞吐结果'
    if invalid_down:
        return '下行测试失败：未获得有效吞吐结果'
    return ''


def target_port_of(cfg):
    """目标机 iperf3 -s 监听的端口 —— 即本次的目标机端口（后端机默认也连它）。

    NAT 商家的端口映射基本都是同号映射（公网 43343 → 内网 43343），
    所以只需要一个端口：目标机监听它，后端机也连它。
    """
    return aj.valid_port(cfg.get('port'), DEFAULT_PARAMS['port'])


def item_port(it, cfg):
    """该后端机本次连接目标机的端口（逐台存了就用存的；0/缺失 = 跟目标机端口一致）。"""
    return aj.valid_port((it or {}).get('port'), target_port_of(cfg))


def item_cfg(cfg, it):
    """把参数换成该后端机的参数（端口逐台不同）。"""
    c = dict(cfg)
    c['port'] = item_port(it, cfg)
    return c


def serve_ports(items, cfg):
    """目标机上需要监听的端口集合。

    目标机端口必须起；此外每台后端机**单独指定过**的连接端口也一起起
    （多机各用各端口、目标机有公网 IP 的场景）。只跟随目标机端口的机器不额外起。
    """
    ports = {target_port_of(cfg)}
    for it in items:
        p = int((it or {}).get('port') or 0)
        if p and p != target_port_of(cfg):
            ports.add(aj.valid_port(p, target_port_of(cfg)))
    return sorted(ports)


# iperf3 server 存活上限：正常收尾由 _worker 的 finally 立即关闭，TTL 只兜底
# 「面板崩溃 / 关闭指令送不到」的情况，所以按本轮预估串行时长给，短测试不再白留半小时。
_SERVER_PREP_SECONDS = 120   # 环境检查、起 server、机器之间切换等固定开销
_SERVER_TAIL_SECONDS = 300   # 余量：任务派发与心跳往返、最后一台的收尾


def server_ttl(items, cfg):
    """本轮目标机 iperf3 server 的兜底存活上限（秒）。

    iperf3 全局串行，所以关键路径 ≈ 台数 ×（上行 + 下行 + 切换）；ping 与后续机器的
    iperf3 并行，最后一台的 ping 落在 server 不再被需要之后，因此不计入关键路径。
    结果经 aj.clamp_ttl 收敛到 [300s, 3600s]：台数 × 时长很大时会被压到上限，
    剩下的靠通道内端口预检自愈重启续期（见 _run_backend）。
    """
    n = max(1, len(items or []))
    lane = n * (2 * int(cfg['duration']) + 10)
    return aj.clamp_ttl(lane + _SERVER_PREP_SECONDS + _SERVER_TAIL_SECONDS)


# 发行版自带 iperf3 常驻服务（Debian/Ubuntu 装包时问「是否作为守护进程启动」，
# 默认 No，但有人答过 Yes 就会有）的提示语：它的命令行是 iperf3 --server --interval 0，
# 不带 -p，且 systemd 单元是 Restart=always —— 面板既杀不掉它，也没法在同端口起自己的 server。
_DISTRO_DAEMON_HINT = (
    '检测到发行版自带的 iperf3 常驻服务（systemd 单元 iperf3.service）：它一直监听 5201，'
    '不受本面板控制，也无法与测试共用端口。处理办法二选一：'
    '① 在目标机执行 systemctl disable --now iperf3（推荐，顺手关掉这个长期对外开放的端口）后重试；'
    '② 或在面板里把「目标机端口」改成别的端口（如 5202）绕开它。')


def parse_daemon_report(out):
    """解析 Agent check_daemon 的输出 → (发行版服务状态, {端口: 占用该端口的进程行})。

    状态取值 active（正在跑）/ enabled（没跑但开机自启）/ inactive（没跑也没自启）/
    absent（没有这个服务）/ unknown（体检任务本身没跑成）。
    """
    m = re.search(r'^DISTRO_DAEMON=(\w+)$', out or '', re.M)
    state = m.group(1) if m else 'unknown'
    busy = {}
    for pm in re.finditer(r'^PORT_BUSY=(\d+) ?(.*)$', out or '', re.M):
        busy[int(pm.group(1))] = pm.group(2).strip()
    return state, busy


def build_iperf_cmd(host, cfg, reverse=False):
    """拼 iperf3 客户端命令（host 由调用方保证是合法 IP，仍然 quote 防注入）。

    -P 线程数、-t 时长、-p 端口可自定义；UDP 追加 -u -b（iperf3 的 -b 是**每流**限速，
    所以 -P 4 -b 100M 等于总目标 400M）；reverse=True 即下行（-R）。
    """
    parts = ['iperf3', '-c', shlex.quote(str(host)),
             '-p', str(cfg['port']), '-t', str(cfg['duration']), '-P', str(cfg['streams'])]
    if cfg['udp']:
        parts += ['-u', '-b', cfg['udp_bandwidth']]
    if reverse:
        parts.append('-R')
    return ' '.join(parts)


class RunAborted(Exception):
    """用户请求停止测试。"""


class LineBuffer:
    """把任意分块的数据按行切开后回调。"""

    def __init__(self, cb):
        self.cb = cb
        self.buf = ''

    def feed(self, text):
        self.buf += text
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            self.cb(line.rstrip('\r'))

    def flush(self):
        if self.buf:
            self.cb(self.buf)
            self.buf = ''


class Iperf3Lane:
    """iperf3 测试串行通道：按机器顺序轮流占用，ping 阶段不占通道。"""

    def __init__(self):
        self.cond = threading.Condition()
        self.turn = 0
        self.stopped = False

    def acquire(self, idx):
        with self.cond:
            while self.turn != idx and not self.stopped:
                self.cond.wait(0.5)
            return not self.stopped

    def release(self):
        with self.cond:
            self.turn += 1
            self.cond.notify_all()

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()


def active_run_id():
    with _state_lock:
        for rid in _active:
            return rid
    return None


def stop_run(run_id):
    st = _active.get(run_id)
    if st:
        st['stop'] = True
        return True
    return False


def get_log(run_id):
    return get_log_data(run_id)['raw_lines']


def get_log_data(run_id):
    """返回结构化事件和完整原始日志，兼容没有事件标记的旧记录。"""
    st = _active.get(run_id)
    if st:
        lines = list(st['log'])
    else:
        run = db.get_run(run_id)
        lines = run['log'].splitlines() if run and run['log'] else []
    events = []
    raw_lines = []
    for line in lines:
        if line.startswith(_EVENT_PREFIX):
            try:
                events.append(json.loads(line[len(_EVENT_PREFIX):]))
            except (TypeError, ValueError):
                raw_lines.append(line)
        else:
            raw_lines.append(line)
    return {'events': events, 'raw_lines': raw_lines}


def proto_name(ip_version):
    return 'IPv6' if int(ip_version or 4) == 6 else 'IPv4'


def start_run(target_id, backend_ids, ip_version=4, params=None,
              source='manual', schedule_id=None, label=''):
    """创建并异步启动一次测试。

    source / schedule_id / label 用于区分「手动测试」与「定时任务触发的测试」，
    定时任务的每轮结果要靠它们回填到对应的对比曲线里。
    """
    cfg = normalize_params(params)
    ip_version = 6 if int(ip_version or 4) == 6 else 4
    proto = proto_name(ip_version)
    with _state_lock:
        if _active:
            raise RuntimeError('已有测试任务正在运行，请等待完成或先停止')
        target = db.get_machine(target_id)
        if not target:
            raise RuntimeError('目标机器不存在')
        if target['role'] != 'target':
            raise RuntimeError(f'机器「{target["name"]}」的角色不是「目标机器」，请先在机器管理中修改角色')
        if not backend_ids:
            raise RuntimeError('请至少选择一台后端机器')
        if not db.machine_online(target):
            raise RuntimeError('目标机器的 Agent 未上线，请先在该机器上执行接入命令并等待其上线')
        # 测试地址：默认 IPv4；勾选 IPv6 后使用目标机的 IPv6 地址
        target_ip = db.machine_test_ip(target, ip_version)
        if not target_ip:
            raise RuntimeError(
                f'目标机器「{target["name"]}」还没有 {proto} 地址，无法进行 {proto} 测试'
                f'（可在机器管理里点「探测地址」重试，或确认该机器有 {proto} 出口）')
        for bid in backend_ids:
            m = db.get_machine(bid)
            if not m:
                raise RuntimeError(f'后端机器 #{bid} 不存在')
            if m['role'] != 'backend':
                raise RuntimeError(f'机器「{m["name"]}」的角色不是「后端机器」，不能作为后端参加测试')
        run_id = db.create_run(target, backend_ids, ip_version, target_host=target_ip,
                               source=source, schedule_id=schedule_id, **cfg)
        _active[run_id] = {'stop': False, 'log': []}
        if label:
            _active[run_id]['log'].append(
                f"[{time.strftime('%H:%M:%S')}] === 本次测试由{label}触发 ===")
    threading.Thread(target=_worker, args=(run_id, target, ip_version), daemon=True).start()
    return run_id


def _job(machine, kind, params, timeout, log, stop_check):
    """向机器派发一个任务并等待完成，流式转发输出，返回 (exit_code, 输出)。"""
    name = machine['name']
    job_id = db.create_job(machine['id'], kind, params, timeout)
    command_preview = job_types.preview(kind, params)
    log(f'[{name}] 派发任务 #{job_id}: {command_preview[:100]}')
    lb = LineBuffer(lambda line: log(f'[{name}] {line}'))
    offset = 0
    start = time.time()
    while True:
        if stop_check is not None:
            stop_check()
        job = db.get_job(job_id)
        out = job['output'] or ''
        if len(out) > offset:
            lb.feed(out[offset:])
            offset = len(out)
        if job['status'] in ('finished', 'failed'):
            lb.flush()
            code = job['exit_code'] if job['exit_code'] is not None else -1
            return code, out
        if job['status'] == 'queued' and time.time() - start > 45:
            db.fail_job(job_id, 'agent 未领取任务（可能离线）')
            raise RuntimeError(f'[{name}] agent 未领取任务（可能离线），请确认机器在线')
        if time.time() - start > timeout + 90:
            db.fail_job(job_id, '任务执行超时')
            raise TimeoutError(f'[{name}] 任务执行超时({timeout}s): {command_preview[:60]}')
        time.sleep(0.8)


def _worker(run_id, target, ip_version=4):
    st = _active[run_id]
    log_lines = st['log']

    def log(msg):
        log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(log_lines) > 4000:
            del log_lines[:len(log_lines) - 4000]

    def event(stage, status, title, summary='', machine='', details=None):
        payload = {
            'time': time.strftime('%H:%M:%S'),
            'stage': stage,
            'status': status,
            'title': title,
            'summary': summary,
            'machine': machine,
            'details': details or {},
        }
        log_lines.append(_EVENT_PREFIX + json.dumps(payload, ensure_ascii=False))

    def stop_check():
        if st['stop']:
            raise RunAborted()

    lane = None
    threads = []
    started_ports = []           # 本轮真正在目标机起起来的 server 端口（收尾必须全部关掉）
    ttl = aj.SERVER_TTL_DEFAULT  # 下面按本轮参数覆盖
    status = 'finished'
    error = ''
    proto = proto_name(ip_version)
    try:
        # 目标地址取自本次测试记录（创建时就已按协议选定并写入），这里只做防御性校验
        run = db.get_run(run_id) or {}
        target_ip = (run.get('target_host') or '').strip()
        try:
            addr = ipaddress.ip_address(target_ip)
        except ValueError:
            raise RuntimeError(
                f'目标机地址无效（{target_ip!r}），请点击机器管理里的「探测地址」后重试')
        if addr.version != ip_version:
            raise RuntimeError(
                f'目标机地址 {target_ip} 与所选协议 {proto} 不一致，请重新选择后再试')
        cfg = run_cfg(run)
        kind = f"UDP（-b {cfg['udp_bandwidth']}/流）" if cfg['udp'] else 'TCP'
        event('precheck', 'running', '预检开始',
              f'目标机 {target["name"]}，协议 {proto}，{kind}')
        log(f"=== 测试开始 | 目标机器: {target['name']} ({target_ip}) | 协议: {proto} | "
            f"{kind} | -P {cfg['streams']} -t {cfg['duration']} | "
            f"ping -c {cfg['ping_count']} ===")
        log('[目标] 通过 Agent 检查/安装 iperf3 与 ping ...')
        code, out = _job(target, 'ensure_tools', {}, 600, log, stop_check)
        if code != 0:
            raise RuntimeError('目标机 iperf3/ping 安装失败: ' + (out or '').strip()[-300:])

        items = db.get_run_items(run_id)
        # 目标机端口（NAT 同号映射时就是商家映射的那个端口）+ 各机单独指定的连接端口
        tport = target_port_of(cfg)
        ports = serve_ports(items, cfg)
        ttl = server_ttl(items, cfg)
        # 开测前体检：发行版自带的 iperf3 常驻服务会一直占着 5201（它的命令行不带 -p，
        # 面板杀不掉也起不来自己的 server），先问清楚，好把提示写进日志和报错里。
        # 体检只是提示性质，自己失败（超时等）不能拖累整轮测试。
        try:
            code, dout = _job(target, 'check_daemon', {'ports': ports},
                              15, log, stop_check)
            daemon_state, busy_ports = (parse_daemon_report(dout) if code == 0
                                        else ('unknown', {}))
        except RunAborted:
            raise
        except Exception as e:
            log(f'[目标] 体检脚本未跑成（不影响测试）: {e}')
            daemon_state, busy_ports = 'unknown', {}
        if daemon_state in ('active', 'enabled'):
            log('[目标] ⚠️ ' + _DISTRO_DAEMON_HINT
                + ('（该服务正在运行）' if daemon_state == 'active' else '（该服务没在跑，但已设为开机自启）'))
        for p, line in sorted(busy_ports.items()):
            log(f'[目标] ⚠️ 端口 {p} 已被其它进程占用：{line[:150]}')
        log(f"[目标] 启动 iperf3 server：监听端口 {'、'.join(str(p) for p in ports)}"
            f"（后端机默认连 {tport}）；server 存活上限 {ttl} 秒"
            f"（正常收尾会立即关闭，此值只兜底面板异常）")
        for p in ports:
            required = (p == tport)
            code, out = _job(
                target, 'start_server',
                {'port': p, 'ttl': ttl, 'bind': target_ip}, 30, log, stop_check)
            if code == 0:
                started_ports.append(p)
                # 立刻落库：面板万一在这之后崩溃/被重启，下次启动能按这个清单回收 server
                db.update_run(run_id, served_ports=' '.join(str(x) for x in started_ports))
                continue
            if required:
                msg = f'目标机 iperf3 -s 启动失败（端口 {p}）: ' + (out or '').strip()[-300:]
                if p in busy_ports:
                    msg += f'；该端口已被占用：{busy_ports[p][:150]}'
                if daemon_state in ('active', 'enabled'):
                    msg += '；' + _DISTRO_DAEMON_HINT
                raise RuntimeError(msg)
            # 后端机单独指定的连接端口：起不来只影响那台机器（预检时会给出明确报错）
            log(f"[目标] ⚠️ 端口 {p} 未能在目标机本机监听（可能已被占用）；"
                f"该端口对应的后端机会在预检时报错")

        log(f"[目标] iperf3 -s 已就绪（端口 {tport}，{'UDP' if cfg['udp'] else 'TCP'}"
            f"{'，' + str(cfg['streams']) + ' 线程' if cfg['streams'] > 1 else ''}），"
            f"开始流水线测试：iperf3 串行 / ping 并行")
        event('precheck', 'done', '预检完成',
              f'目标机端口 {tport} 已就绪，开始连接 {len(items)} 台后端机')

        lane = Iperf3Lane()
        for idx, it in enumerate(items):
            t = threading.Thread(target=_run_backend,
                                 args=(lane, st, log, stop_check, target, it, idx, target_ip,
                                       proto, cfg, ttl, event, run),
                                 daemon=True)
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    except RunAborted:
        status = 'stopped'
        error = '用户手动停止测试'
        event('complete', 'stopped', '测试已停止', error)
        log('=== 测试被手动停止 ===')
        if lane:
            lane.stop()
        for t in threads:
            t.join(timeout=15)
        db.fail_open_items(run_id, '测试被手动停止')
    except Exception as e:
        status = 'failed'
        error = str(e) or e.__class__.__name__
        event('complete', 'failed', '测试失败', error)
        log(f'[致命错误] {error}')
        log(traceback.format_exc()[-800:])
        if lane:
            lane.stop()
        for t in threads:
            t.join(timeout=15)
        db.fail_open_items(run_id, '目标机异常，未执行')
    finally:
        if lane:
            lane.stop()
        for t in threads:
            t.join(timeout=15)

        # 任何退出路径（正常结束 / 手动停止 / 异常中断）都必须关掉目标机的 iperf3 server，
        # 否则端口会一直开到 TTL 到点，期间可能被扫描到并盗刷流量。
        if started_ports:
            log('=== 正在关闭目标机 iperf3 server（端口 '
                + '、'.join(str(p) for p in started_ports) + '）===')
            try:
                _job(target, 'stop_server', {'ports': started_ports}, 15, log, None)
            except Exception as e:
                log(f'[目标] ⚠️ 关闭 iperf3 server 失败（{e}）；'
                    f'残留进程会在 {ttl} 秒存活上限到点后自动退出')

        run = db.get_run(run_id)
        items = db.get_run_items(run_id)
        db.cancel_queued_jobs([it['machine_id'] for it in items if it['machine_id']] +
                              [target['id']])
        total = len(items)
        done = sum(1 for i in items if i['status'] == 'done')
        if status == 'finished':
            if total and done == total:
                status = 'finished'
            elif done:
                status = 'partial'
            else:
                status = 'failed'
        target_info = {
            'name': run['target_name'], 'host': run['target_host'],
            'region': run['target_region'], 'bandwidth': run['target_bandwidth'],
        }
        summary = quality.run_summary(dict(run, status=status), items)
        analysis_status = ('failed' if summary['overall_code'] in ('failed', 'partial')
                           else 'warning' if summary['overall_code'] == 'attention'
                           else 'done')
        event('analysis', analysis_status, '结果分析',
              f"成功 {summary['success_count']} 台，失败 {summary['failure_count']} 台，"
              f"需关注 {summary['attention_count']} 台",
              details=summary)
        event('complete', status if status in ('finished', 'failed', 'partial', 'stopped') else 'done',
              '测试完成' if status != 'failed' else '测试结束',
              summary['overall_label'], details=summary)
        report = quality.build_report(run, target_info, items)
        log(f'=== 测试结束，状态: {status}，报告已生成 ===')
        db.finish_run(run_id, status=status, report=report,
                      log_text='\n'.join(log_lines), error=error)
        with _state_lock:
            _active.pop(run_id, None)


def _run_backend(lane, st, log, stop_check, target, it, idx, target_ip, proto='IPv4', cfg=None,
                 ttl=aj.SERVER_TTL_DEFAULT, event=None, run=None):
    """单台后端的流水线：环境检查（并行）→ 等通道 → iperf3 上/下行（串行）→ ping（并行）。

    iperf3 / ping 都用目标地址字面量决定协议族（IPv4 或 IPv6），不再依赖本机默认路由；
    线程数 / 时长 / 端口 / TCP-UDP 均来自本次测试的参数（cfg）；ttl 用于目标机 server
    掉线自愈重启时沿用同一个存活上限。
    """
    cfg = cfg or dict(DEFAULT_PARAMS)
    iid = it['id']
    name = it['machine_name']
    # 该后端机本次用的端口（逐台可不同），单独的 cfg 供命令拼装使用
    my_port = item_port(it, cfg)
    my_cfg = dict(cfg, port=my_port)
    db.update_item(iid, status='running', phase='检查环境')
    event = event or (lambda *args, **kwargs: None)
    log(f"--- 后端 {name} ({it['machine_host']}) 开始 ---")
    lane_passed = False
    ping_raw = up_raw = down_raw = ''
    try:
        m = db.get_machine(it['machine_id']) if it['machine_id'] else None
        if not m:
            raise RuntimeError('机器已被删除，无法执行')
        if not db.machine_online(m):
            raise RuntimeError('该机器 Agent 离线，无法执行测试')

        # 环境检查与其他机器并行，互不干扰
        code, out = _job(m, 'ensure_tools', {}, 600, log, stop_check)
        if code != 0:
            raise RuntimeError('iperf3/ping 检查安装失败: ' + (out or '').strip()[-300:])

        tq = target_ip
        # 防御性校验：目标地址必须是合法 IP，避免任何路径上的命令注入
        try:
            ipaddress.ip_address(tq)
        except ValueError:
            raise RuntimeError(f'目标机地址无效（{tq!r}），请让 Agent 重新接入以更新 IP')

        # 等待 iperf3 通道（前一台的 iperf3 结束后立刻轮到本机，ping 不占通道）
        db.update_item(iid, phase='等待 iperf3 通道')
        if not lane.acquire(idx):
            raise RunAborted()
        lane_passed = True
        try:
            # 通道内先预检本机要用的端口；不通则尝试自动重启目标机该端口的 server
            code, out = _job(m, 'check_port', {'host': tq, 'port': my_port},
                             15, log, stop_check)
            if code != 0:
                log(f"[{name}] {my_port} 端口不通，尝试重启目标机 iperf3 server ...")
                code2, out2 = _job(
                    target, 'start_server',
                    {'port': my_port, 'ttl': ttl, 'bind': target_ip},
                    30, log, stop_check)
                if code2 != 0:
                    raise RuntimeError('目标机 iperf3 server 启动失败: ' + (out2 or '').strip()[-200:])
                code, out = _job(m, 'check_port', {'host': tq, 'port': my_port},
                                 15, log, stop_check)
                if code != 0:
                    hints = ''
                    if proto == 'IPv6':
                        hints += '；IPv6 测试需本机与目标机均具备 IPv6 连通性'
                    if cfg['udp']:
                        hints += f'；UDP 测试需同时放行 {my_port}/UDP'
                    raise RuntimeError(
                        f"目标机 {tq} 的 {my_port} 端口从本机不可达"
                        f"（请检查目标机防火墙/安全组/NAT 是否放行并映射到 {my_port}{hints}）")
            event('precheck', 'done', f'{name} 预检完成',
                  f'目标端口 {my_port} 可达', machine=name)

            cmd_up = build_iperf_cmd(tq, my_cfg)
            db.update_item(iid, phase='上行测试')
            log(f'[{name}] $ {cmd_up}   （{proto}）')
            code, up_raw = _job(
                m, 'iperf_client',
                {'host': tq, 'port': my_port, 'duration': cfg['duration'],
                 'streams': cfg['streams'], 'udp': cfg['udp'],
                 'udp_bandwidth': cfg['udp_bandwidth'], 'reverse': False},
                cfg['duration'] + 60, log, stop_check)
            if code != 0:
                raise RuntimeError('上行测试失败: ' + (up_raw or '').strip()[-200:])
            up_s, up_r = quality.parse_iperf(up_raw)
            event('uplink', 'done', f'{name} 上行完成',
                  f"发送 {quality.fmt_num(up_s['mbits'] if up_s else None)} Mbit/s，"
                  f"接收 {quality.fmt_num(up_r['mbits'] if up_r else None)} Mbit/s",
                  machine=name)
            time.sleep(1)

            cmd_down = build_iperf_cmd(tq, my_cfg, reverse=True)
            db.update_item(iid, phase='下行测试')
            log(f'[{name}] $ {cmd_down}   （{proto}）')
            code, down_raw = _job(
                m, 'iperf_client',
                {'host': tq, 'port': my_port, 'duration': cfg['duration'],
                 'streams': cfg['streams'], 'udp': cfg['udp'],
                 'udp_bandwidth': cfg['udp_bandwidth'], 'reverse': True},
                cfg['duration'] + 60, log, stop_check)
            if code != 0:
                raise RuntimeError('下行测试失败: ' + (down_raw or '').strip()[-200:])
            down_s, down_r = quality.parse_iperf(down_raw)
            event('downlink', 'done', f'{name} 下行完成',
                  f"发送 {quality.fmt_num(down_s['mbits'] if down_s else None)} Mbit/s，"
                  f"接收 {quality.fmt_num(down_r['mbits'] if down_r else None)} Mbit/s",
                  machine=name)
        finally:
            lane.release()  # 无论如何立刻让出通道，下一台马上开始 iperf3
        time.sleep(1)

        # ping 不占通道，与后续机器的 iperf3 并行
        cmd_ping = f'ping -c {cfg["ping_count"]} -i 1 {shlex.quote(tq)}'
        db.update_item(iid, phase=f"ping {cfg['ping_count']}次（约{cfg['ping_count']}秒）")
        log(f"[{name}] $ {cmd_ping}  （约需 {cfg['ping_count']} 秒，与其它机器的 iperf3 并行）")
        code, ping_raw = _job(m, 'ping', {'host': tq, 'count': cfg['ping_count']},
                              cfg['ping_count'] + 120, log, stop_check)
        if 'packets transmitted' not in (ping_raw or ''):
            raise RuntimeError('ping 失败: ' + (ping_raw or '').strip()[-200:])

        metrics = quality.parse_metrics(ping_raw, up_raw, down_raw)
        invalid = bidirectional_failure(metrics)
        if invalid:
            raise RuntimeError(invalid)
        quality.evaluate(metrics, it, run=run)
        db.update_item(iid, status='done', phase='完成', ping_raw=ping_raw, up_raw=up_raw,
                       down_raw=down_raw,
                       metrics=json.dumps(metrics, ensure_ascii=False))
        event('ping', 'done', f'{name} Ping 完成',
              f"丢包 {quality.fmt_num(metrics.get('loss_pct'), 2)}%，"
              f"RTT {quality.fmt_num(metrics.get('rtt_avg'))} ms，"
              f"抖动 {quality.fmt_num(metrics.get('mdev_ms'), 2)} ms",
              machine=name)
        extra = ''
        if metrics.get('udp'):
            extra = (f"| UDP 丢包 {quality.fmt_num(metrics.get('up_udp_loss_pct'), 2)}%/"
                     f"{quality.fmt_num(metrics.get('down_udp_loss_pct'), 2)}% ")
        log(f"[{name}] ✅ 完成 | 上行 {quality.fmt_num(metrics.get('up_mbits'))} Mbit/s "
            f"| 下行 {quality.fmt_num(metrics.get('down_mbits'))} Mbit/s "
            f"| 丢包 {quality.fmt_num(metrics.get('loss_pct'))}% " + extra
            + f"| 评价: {metrics.get('rating')}")
    except RunAborted:
        failed_metrics = quality.failure_metrics(it, '手动停止', run=run)
        db.update_item(iid, status='failed', error='手动停止', ping_raw=ping_raw,
                       up_raw=up_raw, down_raw=down_raw,
                       metrics=json.dumps(failed_metrics, ensure_ascii=False))
        event('complete', 'stopped', f'{name} 已停止', '用户手动停止', machine=name)
        log(f'[{name}] ⏹ 已停止')
    except Exception as e:
        failed_metrics = quality.failure_metrics(it, str(e), run=run)
        db.update_item(iid, status='failed', error=str(e), ping_raw=ping_raw,
                       up_raw=up_raw, down_raw=down_raw,
                       metrics=json.dumps(failed_metrics, ensure_ascii=False))
        event('complete', 'failed', f'{name} 测试失败', str(e), machine=name)
        log(f'[{name}] ❌ 失败: {e}')
    finally:
        if not lane_passed:
            # 失败/中止也要把轮次让出去，避免阻塞后面的机器
            if lane.acquire(idx):
                lane.release()


# ---------------- 地址探测（后台补齐机器的 IPv4 / IPv6） ----------------

DISCOVER_COOLDOWN = 600   # 同一台机器两次自动探测的最小间隔（秒）
DISCOVER_COOLDOWN_EMPTY = 6 * 3600   # 探测不到任何全局地址（NAT 机）后的退避间隔
_discover_next = {}       # machine_id -> 下次允许自动探测的时间戳


def start_discovery_worker():
    """启动后台线程：为地址不全的在线机器补一次本机地址探测。

    走 Agent 任务队列（Agent 无需升级）：脚本只在本机查地址，不访问外网。
    面板只监听 IPv4 时也能拿到机器的 IPv6，从而支持 IPv6 测试。
    """
    threading.Thread(target=_discovery_loop, daemon=True).start()


def _discovery_loop():
    while True:
        try:
            _discovery_pass()
        except Exception:
            pass
        time.sleep(5)


def _discovery_pass():
    if active_run_id():
        return                      # 测试进行中不打扰
    now = time.time()
    for m in db.get_machines():
        if m['ip4'] and m['ip6']:
            continue                # 两族地址都已知
        if not db.machine_online(m):
            continue
        if now < _discover_next.get(m['id'], 0):
            continue
        _discover_next[m['id']] = now + DISCOVER_COOLDOWN
        queue_discovery(m['id'])


# 面板重启后回收「孤儿 iperf3 server」：上一次重启把测试从中间掐断时，关闭指令
# 可能没送到，目标机上会留一个裸 server 占着端口（下一轮必然启动失败）。
# 这里按测试记录里存的端口清单，等机器一上线就精确回收（只认本面板自己的模式）。
_ORPHAN_RETRY_SECONDS = 60
_orphan_next = {}                 # run_id -> 下次允许重试回收的时间戳


def clear_orphan_servers(force=False):
    """回收「面板重启时被中断」遗留的目标机 iperf3 server；返回回收成功的 run 列表。"""
    done = []
    for rec in db.runs_with_orphan_ports():
        mid = rec['target_id']
        m = db.get_machine(mid)
        if not m or not db.machine_online(m):
            continue
        if not force and time.time() < _orphan_next.get(rec['id'], 0):
            continue
        ports = [aj.valid_port(p, None) for p in str(rec['served_ports']).split()]
        ports = [p for p in ports if p]
        if not ports:
            db.mark_orphan_ports_cleared(rec['id'])
            continue
        try:
            _job(m, 'stop_server', {'ports': ports}, 15, lambda _s: None, None)
        except Exception as e:
            _orphan_next[rec['id']] = time.time() + _ORPHAN_RETRY_SECONDS
            print(f'[回收] 机器「{m["name"]}」端口 {ports} 的残留 server 未回收：{e}', flush=True)
            continue
        db.mark_orphan_ports_cleared(rec['id'])
        print(f'[回收] 已清理机器「{m["name"]}」上残留的 iperf3 server（端口 {ports}）', flush=True)
        done.append(rec['id'])
    return done


def start_orphan_reclaimer():
    """后台线程：面板启动后回收上一次重启遗留的目标机 server（独立于地址探测，互不阻塞）。"""
    def run():
        for attempt in range(30):        # 机器可能还没上线，等它；最多重试 30 轮
            try:
                if not active_run_id() and clear_orphan_servers():
                    break
                if not db.runs_with_orphan_ports():
                    break
            except Exception as e:
                print(f'[回收] 异常（不影响其它功能）：{e}', flush=True)
            time.sleep(20 if attempt < 3 else 60)
        # 之后交给定期巡检兜底（机器离线久了才上线的情况）
        while True:
            time.sleep(300)
            try:
                if not active_run_id():
                    clear_orphan_servers()
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()


def queue_discovery(mid, force=False):
    """异步派发一次地址探测（不阻塞请求线程）。force=True 时覆盖已存地址。"""
    def run():
        try:
            found = discover_machine(mid, force=force)
            print(f'[探测] 机器 #{mid} 地址: {found or "未获取到全局地址"}', flush=True)
            if not found:
                # 本机只有私网 / ULA 地址（NAT 机典型情况）：不必每 10 分钟探一次
                _discover_next[mid] = time.time() + DISCOVER_COOLDOWN_EMPTY
        except Exception as e:
            reason = _probe_fail_reason(e)
            try:
                db.set_machine_probe(mid, reason)
            except Exception:
                pass
            print(f'[探测] 机器 #{mid} 失败: {reason}', flush=True)
    threading.Thread(target=run, daemon=True).start()


def _probe_fail_reason(e):
    """把探测失败转成面板上能看懂、能照着排查的一句话。"""
    msg = str(e)
    if '超时' in msg or '未领取' in msg:
        return ('地址探测任务没有回传结果：Agent 可能没在跑任务或拒收了任务，'
                '到机器上执行 tail -5 /var/lib/linkpilot/agent.log 查看原因')
    return msg[:200]


def discover_machine(mid, force=False):
    """派发地址探测任务并解析结果。force=True 时用探测值覆盖已存地址（手动刷新）。"""
    m = db.get_machine(mid)
    if not m:
        raise RuntimeError('机器不存在')
    if not db.machine_online(m):
        raise RuntimeError(f'机器「{m["name"]}」的 Agent 未上线，无法探测地址')
    code, out = _job(m, 'report_ips', {}, 20, lambda _s: None, None)
    found = aj.parse_ip_report(out)
    if found:
        db.set_machine_ips(mid, found, force=force)
        db.set_machine_probe(mid, '')
    else:
        # 不是故障：NAT 机的本机地址本来就是私网，测试用的是面板观测到的**出口公网 IP**。
        # 这条只是说明「本机探测没给出可用的新地址（通常也没有全局 IPv6）」。
        db.set_machine_probe(
            mid, '本机探测只有私网 / ULA / 链路本地地址（NAT 机属正常，测试用面板观测到的出口 IP），'
                 '也没有探测到可用于测试的全局 IPv6')
    return found
