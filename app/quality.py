"""iperf3 / ping 输出解析、线路质量评价、Markdown 报告生成。"""
import json
import re

# iperf3 汇总行。TCP： [  5] 0.00-10.00 sec 163 MBytes 137 Mbits/sec 0        sender
#               UDP： [  5] 0.00-10.00 sec 116 MBytes 97.3 Mbits/sec 0.015 ms 412/69894 (0.59%) receiver
SUM_RE = re.compile(
    r'^\[\s*(?:\d+|SUM)\s*\]\s+([\d.]+)\s*-\s*([\d.]+)\s+sec\s+'
    r'([\d.]+)\s+([KMG]?Bytes)\s+([\d.]+)\s+([KMG]?bits)/sec'
    r'(?:\s+(\d+))?'                                        # TCP：Retr（接收行通常没有）
    r'(?:\s+([\d.]+)\s+ms\s+(\d+)/(\d+)\s+\(([\d.]+)%\))?'  # UDP：Jitter + Lost/Total(丢包率)
    r'\s+(sender|receiver)\s*$',
    re.IGNORECASE)
# 汇总区表头，如: [ ID] Interval           Transfer     Bitrate         Retr
HEADER_RE = re.compile(r'^\[\s*ID\s*\]')

# 项目自定义的初始经验阈值，不是行业标准。后续应根据真实线路样本校准。
RATING_THRESHOLDS = {
    'loss_pct': {'warn': 0.1, 'bad': 1.0, 'critical': 3.0},
    'rtt_ms': {'warn': 80.0, 'bad': 180.0, 'critical': 350.0},
    'mdev_ms': {'warn': 2.0, 'bad': 5.0, 'critical': 15.0},
    'throughput_pct': {'warn': 85.0, 'bad': 65.0, 'critical': 40.0},
    'retr_per_gib': {'warn': 2.0, 'bad': 10.0, 'critical': 30.0},
    'retr_per_min': {'warn': 1.0, 'bad': 6.0, 'critical': 20.0},
    'udp_loss_pct': {'warn': 0.1, 'bad': 1.0, 'critical': 3.0},
    'udp_jitter_ms': {'warn': 1.0, 'bad': 5.0, 'critical': 15.0},
    'receiver_ratio': {'warn': 0.9, 'bad': 0.7, 'critical': 0.4},
}
RATING_SEVERITY_SCORE = {'ok': 0, 'warn': 1, 'bad': 3, 'critical': 6}
RATING_CODE_INFO = {
    'S': '优秀',
    'A': '良好',
    'B': '一般',
    'C': '较差',
    'F': '失败',
    '?': '数据不足',
}
RATING_CONFIG_NOTE = '评级采用项目自定义初始经验阈值，不是行业标准，应结合真实线路样本持续校准。'


def _to_mbits(val, unit):
    v = float(val)
    u = unit.lower()
    if u.startswith('g'):
        return v * 1000.0
    if u.startswith('k'):
        return v / 1000.0
    if u.startswith('m'):
        return v
    return v / 1000000.0


def _to_mb(val, unit):
    v = float(val)
    u = unit.lower()
    if u.startswith('g'):
        return v * 1024.0
    if u.startswith('k'):
        return v / 1024.0
    return v


def parse_iperf(out):
    """返回 (sender, receiver) 两条汇总行，含 mbits / transfer_mb / retr（TCP）/ 抖动与丢包（UDP）。

    多线程（-P N）时 iperf3 会先逐流打印 sender/receiver 行，最后再给 [SUM] 汇总行；
    这里只保留最后出现的 sender / receiver，即 [SUM] 行（单流时就是那一行）。
    """
    sender = None
    receiver = None
    for line in (out or '').splitlines():
        m = SUM_RE.match(line.strip())
        if not m:
            continue
        entry = {
            'transfer_mb': _to_mb(m.group(3), m.group(4)),
            'mbits': _to_mbits(m.group(5), m.group(6)),
        }
        if m.group(7) is not None:
            entry['retr'] = int(m.group(7))       # UDP 行没有该列，保持 None 表示不适用
        if m.group(8) is not None:
            entry['jitter_ms'] = float(m.group(8))
            entry['lost'] = int(m.group(9))
            entry['total'] = int(m.group(10))
            entry['loss_pct'] = float(m.group(11))
        if m.group(12).lower() == 'sender':
            sender = entry
        else:
            receiver = entry
    return sender, receiver


def summary_block(out):
    """提取 iperf3 输出末尾的汇总区：表头 + sender + receiver 三行。"""
    header = None
    rows = []
    for line in (out or '').splitlines():
        s = line.rstrip()
        if HEADER_RE.match(s.strip()) and 'Interval' in s:
            header = s
        if SUM_RE.match(s.strip()):
            rows.append(s)
    parts = ([header] if header else []) + rows
    return '\n'.join(parts)


def parse_ping(out):
    r = {}
    m = re.search(r'(\d+)\s*packets transmitted', out or '')
    if m:
        r['pkts_transmitted'] = int(m.group(1))
    m = re.search(r'(\d+)\s*received', out or '')
    if m:
        r['pkts_received'] = int(m.group(1))
    m = re.search(r'([\d.]+)%\s*packet loss', out or '')
    if m:
        r['loss_pct'] = float(m.group(1))
    m = re.search(r'=\s*([\d.]+)\/([\d.]+)\/([\d.]+)\/([\d.]+)\s*ms', out or '')
    if m:
        r['rtt_min'] = float(m.group(1))
        r['rtt_avg'] = float(m.group(2))
        r['rtt_max'] = float(m.group(3))
        r['mdev_ms'] = float(m.group(4))
    return r


def ping_block(out):
    """提取 ping 汇总两行：transmitted... / rtt min/avg/max/mdev..."""
    keep = []
    for line in (out or '').splitlines():
        s = line.strip()
        if 'packets transmitted' in s or s.startswith('rtt ') or s.startswith('round-trip '):
            keep.append(s)
    return '\n'.join(keep)


def parse_metrics(ping_raw, up_raw, down_raw):
    """把三段原始输出解析为结构化指标（含报告用的三段摘录块）。"""
    mt = parse_ping(ping_raw)
    us, ur = parse_iperf(up_raw)
    ds, dr = parse_iperf(down_raw)
    mt['up_sender_mbits'] = us['mbits'] if us else None
    mt['up_receiver_mbits'] = ur['mbits'] if ur else None
    mt['down_sender_mbits'] = ds['mbits'] if ds else None
    mt['down_receiver_mbits'] = dr['mbits'] if dr else None
    mt['up_sender_transfer_mb'] = us['transfer_mb'] if us else None
    mt['up_receiver_transfer_mb'] = ur['transfer_mb'] if ur else None
    mt['down_sender_transfer_mb'] = ds['transfer_mb'] if ds else None
    mt['down_receiver_transfer_mb'] = dr['transfer_mb'] if dr else None
    transfers = [mt.get('up_sender_transfer_mb'), mt.get('down_sender_transfer_mb')]
    transfers = [float(v) for v in transfers if v is not None]
    mt['transfer_total_mb'] = sum(transfers) if transfers else None
    # 上行取正向测试的 sender，下行取 -R 测试的 receiver
    mt['up_mbits'] = mt['up_sender_mbits']
    mt['down_mbits'] = mt['down_receiver_mbits']
    retrs = [e['retr'] for e in (us, ds) if e and e.get('retr') is not None]
    mt['retr_total'] = sum(retrs) if retrs else None
    # UDP：丢包与抖动只有接收端统计得准（发送端恒为 0），上下行都取 receiver 那一行
    for tag, rcv in (('up', ur), ('down', dr)):
        udp = rcv if (rcv and 'loss_pct' in rcv) else None
        mt[f'{tag}_udp_loss_pct'] = udp['loss_pct'] if udp else None
        mt[f'{tag}_jitter_ms'] = udp['jitter_ms'] if udp else None
        mt[f'{tag}_udp_lost'] = udp['lost'] if udp else None
    mt['udp'] = any(mt.get(k) is not None for k in ('up_udp_loss_pct', 'down_udp_loss_pct'))
    mt['ping_block'] = ping_block(ping_raw)
    mt['up_block'] = summary_block(up_raw)
    mt['down_block'] = summary_block(down_raw)
    return mt


def fmt_num(v, nd=1):
    if v is None:
        return '—'
    s = f'{float(v):.{nd}f}'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


def parse_bw(label):
    """解析带宽标记为 Mbit/s，如 '500M'->500, '1G'->1000, '不限'->None。"""
    if not label:
        return None
    m = re.search(r'(\d+(?:\.\d+)?)\s*([GgMm])?', str(label))
    if not m:
        return None
    v = float(m.group(1))
    u = (m.group(2) or 'm').lower()
    return v * 1000.0 if u == 'g' else v


def _lower_better_severity(value, thresholds):
    if value is None:
        return 'unknown'
    if value <= thresholds['warn']:
        return 'ok'
    if value <= thresholds['bad']:
        return 'warn'
    if value <= thresholds['critical']:
        return 'bad'
    return 'critical'


def _higher_better_severity(value, thresholds):
    if value is None:
        return 'unknown'
    if value >= thresholds['warn']:
        return 'ok'
    if value >= thresholds['bad']:
        return 'warn'
    if value >= thresholds['critical']:
        return 'bad'
    return 'critical'


def _grade_for_score(score):
    if score == 0:
        return 'S'
    if score <= 3:
        return 'A'
    if score <= 8:
        return 'B'
    return 'C'


def _issue_text(tag):
    return {
        '存在丢包': '先检查链路拥塞、目标机防火墙和端口限速。',
        'RTT 偏高': '核对跨区路由、目标机负载和中间网络延迟。',
        '抖动偏高': '重点检查拥塞、队列和 NAT 设备稳定性。',
        '重传偏高': '结合丢包与拥塞情况排查链路质量和限速。',
        '上行吞吐偏低': '核对上行标称带宽、限速和发送端负载。',
        '下行吞吐偏低': '核对下行标称带宽、限速和接收端负载。',
        '传输损耗': '检查接收端是否存在丢包、拥塞或链路异常。',
        'UDP 丢包偏高': '检查 UDP 限速、防火墙和链路拥塞。',
        'UDP 抖动偏高': '检查实时队列和中间网络稳定性。',
        '吞吐基线缺失': '请为机器填写带宽标记，才能判断标称吞吐达成率。',
        '关键指标缺失': '查看原始输出并复测，避免用不完整数据判断线路质量。',
        '测试失败': '查看原始错误和阶段事件，优先恢复测试链路。',
    }.get(tag, '查看诊断详情和原始输出进一步定位。')


def evaluate(mt, machine, run=None):
    """按初始经验阈值计算 S/A/B/C/F 评级和结构化诊断。

    所有阈值集中在 RATING_THRESHOLDS，仅用于项目内部比较，不代表行业标准。
    缺失关键指标时返回“数据不足”，不会按 0 或优秀处理。
    """
    run = run or {}
    diagnostics = []
    tags = []
    issues = []
    score = 0
    missing = []

    def add(name, status, text, tag=None, value=None):
        nonlocal score
        diagnostics.append({'name': name, 'status': status, 'text': text, 'value': value})
        score += RATING_SEVERITY_SCORE.get(status, 0)
        if tag and tag not in tags:
            tags.append(tag)
        if tag and status in ('warn', 'bad', 'critical'):
            issues.append({'tag': tag, 'severity': status,
                           'score': RATING_SEVERITY_SCORE[status]})

    loss = mt.get('loss_pct')
    if loss is None:
        missing.append('ping 丢包率')
    else:
        sev = _lower_better_severity(loss, RATING_THRESHOLDS['loss_pct'])
        add('丢包率', sev, f'ping 丢包 {fmt_num(loss, 2)}%', '存在丢包' if sev != 'ok' else None, loss)

    rtt = mt.get('rtt_avg')
    if rtt is None:
        missing.append('RTT')
    else:
        sev = _lower_better_severity(rtt, RATING_THRESHOLDS['rtt_ms'])
        add('RTT', sev, f'RTT 平均 {fmt_num(rtt)} ms', 'RTT 偏高' if sev != 'ok' else None, rtt)

    mdev = mt.get('mdev_ms')
    if mdev is None:
        missing.append('抖动 mdev')
    else:
        sev = _lower_better_severity(mdev, RATING_THRESHOLDS['mdev_ms'])
        add('抖动', sev, f'ping 抖动 {fmt_num(mdev, 2)} ms',
            '抖动偏高' if sev != 'ok' else None, mdev)

    up = mt.get('up_mbits')
    down = mt.get('down_mbits')
    if up is None or float(up or 0) <= 0:
        missing.append('上行吞吐')
    if down is None or float(down or 0) <= 0:
        missing.append('下行吞吐')

    labeled = parse_bw(machine.get('bandwidth') or machine.get('machine_bandwidth'))
    if not labeled:
        tags.append('吞吐基线缺失')
        diagnostics.append({
            'name': '吞吐达成率', 'status': 'unknown', 'value': None,
            'text': '机器未填写带宽标记，无法计算标称吞吐达成率',
        })
    else:
        for direction, val in (('上行', up), ('下行', down)):
            if val is None or float(val) <= 0:
                continue
            pct = float(val) / labeled * 100.0
            sev = _higher_better_severity(pct, RATING_THRESHOLDS['throughput_pct'])
            tag = f'{direction}吞吐偏低' if sev != 'ok' else None
            add(f'{direction}吞吐', sev,
                f'{direction} {fmt_num(val)} Mbit/s，约为标称 {labeled:g}M 的 {pct:.0f}%',
                tag, pct)

    retr = mt.get('retr_total')
    transfer_mb = mt.get('transfer_total_mb')
    if not mt.get('udp'):
        if retr is None or transfer_mb is None or float(transfer_mb or 0) <= 0:
            missing.append('TCP 重传/传输量')
        else:
            duration = max(1.0, float(run.get('duration') or 10))
            per_gib = float(retr) / (float(transfer_mb) / 1024.0)
            per_min = float(retr) / (duration / 60.0)
            sev_gib = _lower_better_severity(per_gib, RATING_THRESHOLDS['retr_per_gib'])
            sev_min = _lower_better_severity(per_min, RATING_THRESHOLDS['retr_per_min'])
            rank = {'ok': 0, 'warn': 1, 'bad': 2, 'critical': 3}
            sev = max((sev_gib, sev_min), key=lambda x: rank.get(x, 0))
            add('重传', sev,
                f'重传 {int(retr)} 次，约 {fmt_num(per_gib, 2)} 次/GiB、'
                f'{fmt_num(per_min, 2)} 次/分钟',
                '重传偏高' if sev != 'ok' else None, per_gib)

        us_, ur_ = mt.get('up_sender_mbits'), mt.get('up_receiver_mbits')
        ds_, dr_ = mt.get('down_sender_mbits'), mt.get('down_receiver_mbits')
        if us_ and ur_:
            ratio = float(ur_) / float(us_)
            sev = _higher_better_severity(ratio, RATING_THRESHOLDS['receiver_ratio'])
            add('上行接收率', sev, f'上行接收率 {ratio * 100:.1f}%',
                '传输损耗' if sev != 'ok' else None, ratio)
        if ds_ and dr_:
            ratio = float(dr_) / float(ds_)
            sev = _higher_better_severity(ratio, RATING_THRESHOLDS['receiver_ratio'])
            add('下行接收率', sev, f'下行接收率 {ratio * 100:.1f}%',
                '传输损耗' if sev != 'ok' else None, ratio)
    else:
        udp_losses = [v for v in (mt.get('up_udp_loss_pct'), mt.get('down_udp_loss_pct'))
                      if v is not None]
        udp_jitters = [v for v in (mt.get('up_jitter_ms'), mt.get('down_jitter_ms'))
                       if v is not None]
        if not udp_losses:
            missing.append('UDP 丢包')
        else:
            worst = max(udp_losses)
            sev = _lower_better_severity(worst, RATING_THRESHOLDS['udp_loss_pct'])
            add('UDP 丢包', sev, f'UDP 最大丢包 {fmt_num(worst, 2)}%',
                'UDP 丢包偏高' if sev != 'ok' else None, worst)
        if not udp_jitters:
            missing.append('UDP 抖动')
        else:
            worst = max(udp_jitters)
            sev = _lower_better_severity(worst, RATING_THRESHOLDS['udp_jitter_ms'])
            add('UDP 抖动', sev, f'UDP 最大抖动 {fmt_num(worst, 2)} ms',
                'UDP 抖动偏高' if sev != 'ok' else None, worst)

    missing = list(dict.fromkeys(missing))
    if missing:
        if '关键指标缺失' not in tags:
            tags.append('关键指标缺失')
        diagnostics.append({
            'name': '数据完整性', 'status': 'critical',
            'text': '缺少关键指标：' + '、'.join(missing), 'value': missing,
        })

    if missing or not labeled:
        code = '?'
    else:
        code = _grade_for_score(score)
    label = RATING_CODE_INFO[code]
    needs_attention = code in ('B', 'C', 'F', '?') or bool(issues)
    if code == '?':
        summary = '关键指标或吞吐基线不完整，暂不能给出可靠等级，请查看诊断并复测。'
    elif code == 'S':
        summary = '关键指标均处于当前经验阈值内，未发现明显异常。'
    else:
        primary = max(issues, key=lambda x: x['score']) if issues else None
        summary = _issue_text(primary['tag']) if primary else '存在质量波动，建议查看诊断详情。'
    detail = '；'.join(d['text'] for d in diagnostics)
    mt.update({
        'rating_code': code,
        'rating': label,
        'rating_label': label,
        'rating_tags': tags,
        'rating_summary': summary,
        'rating_detail': detail or '—',
        'rating_diagnostics': diagnostics,
        'rating_penalty': score,
        'needs_attention': needs_attention,
        'rating_threshold_note': RATING_CONFIG_NOTE,
    })
    return mt


def failure_metrics(machine, error, run=None):
    """失败或中止时仍生成统一评级结构，供汇总表和报告使用。"""
    message = str(error or '测试失败')
    machines = machine.get('machine_name') or machine.get('name') or '后端机器'
    diagnostics = [
        {'name': '测试状态', 'status': 'critical', 'value': None,
         'text': f'{machines}：{message}'},
    ]
    return {
        'ping_block': '', 'up_block': '', 'down_block': '',
        'rating_code': 'F',
        'rating': '失败',
        'rating_label': '失败',
        'rating_tags': ['测试失败'],
        'rating_summary': '测试失败，未获得可用于线路质量判断的完整结果。',
        'rating_detail': message,
        'rating_diagnostics': diagnostics,
        'rating_penalty': None,
        'needs_attention': True,
        'rating_threshold_note': RATING_CONFIG_NOTE,
        'duration': int((run or {}).get('duration') or 0),
    }


def run_summary(run, items):
    """根据实际 run_items 汇总成功、失败和“成功但需关注”的机器数。"""
    total = len(items)
    success = failure = attention = 0
    for item in items:
        metrics = item.get('metrics_obj')
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except (TypeError, ValueError):
                metrics = None
        up = (metrics or {}).get('up_mbits')
        down = (metrics or {}).get('down_mbits')
        valid = (item.get('status') == 'done' and up is not None and down is not None
                 and float(up or 0) > 0 and float(down or 0) > 0)
        if not valid:
            failure += 1
            continue
        success += 1
        code = (metrics or {}).get('rating_code') or {
            '优秀': 'S', '良好': 'A', '一般': 'B', '较差': 'C',
            '失败': 'F', '数据不足': '?',
        }.get((metrics or {}).get('rating'))
        if code in ('B', 'C', 'F', '?') or (metrics or {}).get('needs_attention'):
            attention += 1

    run_status = (run or {}).get('status') or 'pending'
    if run_status in ('running', 'pending'):
        code, label = 'running', '测试进行中'
    elif total and failure == total:
        code, label = 'failed', '测试失败'
    elif failure:
        code, label = 'partial', '部分失败'
    elif attention:
        code, label = 'attention', '完成，需关注'
    else:
        code, label = 'finished', '全部正常'
    return {
        'target_name': (run or {}).get('target_name') or '—',
        'backend_total': total,
        'success_count': success,
        'failure_count': failure,
        'attention_count': attention,
        'overall_code': code,
        'overall_label': label,
    }


def mask_ip(ip):
    """报告脱敏：IPv4 只保留 A.B 两段（C/D 段打码）；IPv6 保留前两组。"""
    if not ip:
        return ip
    parts = ip.split('.')
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return f'{parts[0]}.{parts[1]}.*.*'
    m = re.match(r'^([0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}):', ip)
    if m:
        return m.group(1) + ':****'
    return ip


# 兜底打码：对整份报告文本生效，覆盖历史报告与原始数据中残留的 IP
_IPV4_RE = re.compile(r'\b(\d{1,3}\.\d{1,3})\.\d{1,3}\.\d{1,3}\b')


def mask_report_text(text):
    if not text:
        return text
    return _IPV4_RE.sub(r'\1.*.*', text)


def run_params_text(run):
    """报告里的「测试参数」描述（旧记录没有这些列时退回默认值）。"""
    streams = int(run.get('streams') or 1)
    duration = int(run.get('duration') or 10)
    port = int(run.get('port') or 5201)
    ping_count = int(run.get('ping_count') or 200)
    kind = ('UDP（-u，目标带宽 %s/流）' % (run.get('udp_bandwidth') or '100M')
            if int(run.get('udp') or 0) else 'TCP')
    return (f'iperf3 {streams} 线程（-P）× 上行 / 下行（-R）各 {duration} 秒（-t），'
            f'目标机端口 {port}（后端机默认连它），{kind}；`ping -c {ping_count} -i 1`；'
            f'iperf3 全局串行，ping 与其它机器的 iperf3 并行')


def build_report(run, target, items):
    """生成 Markdown 报告：汇总表（含线路质量评价列）+ 每台机器的原始摘录。"""
    proto = 'IPv6' if int(run.get('ip_version') or 4) == 6 else 'IPv4'
    udp = bool(int(run.get('udp') or 0))
    ncols = 11 if udp else 10
    lines = []
    lines.append('# iperf3 线路质量测试报告')
    lines.append('')
    lines.append(
        f"- **目标机器**：{target['name']}"
        f"（{target.get('region') or '地区未标记'} · {target.get('bandwidth') or '带宽未标记'} · `{mask_ip(target.get('host'))}`）")
    lines.append(f"- **测试协议**：{proto}（后端发起端与目标被测端均使用 {proto}）")
    lines.append(f"- **测试参数**：{run_params_text(run)}")
    default_port = int(run.get('port') or 5201)
    port_pairs = [(it['machine_name'], int(it.get('port') or default_port)) for it in items]
    if len({p for _, p in port_pairs}) > 1:
        # 每台后端机可以用各自的端口连目标机（目标机会按用到的端口分别起 server）
        lines.append('- **各机连接端口**：'
                     + ' · '.join(f'{name} {port}' for name, port in port_pairs))
    lines.append(f"- **测试时间**：{run['created_at']} ~ {run.get('finished_at') or ''}")
    lines.append('')
    if udp:
        lines.append('| 后端机器 | 地区 | 带宽 | ping 丢包 | RTT avg | ping 抖动 | 上行 '
                     '| 下行 | UDP 丢包 上/下 | UDP 抖动 上/下 | 线路质量评价 |')
        lines.append('|---|---|---|---|---|---|---|---|---|---|---|')
    else:
        lines.append('| 后端机器 | 地区 | 带宽 | 丢包率 | RTT avg | 抖动 mdev | 上行 '
                     '| 下行 | 重传 | 线路质量评价 |')
        lines.append('|---|---|---|---|---|---|---|---|---|---|')
    for it in items:
        name = it['machine_name']
        region = it['machine_region'] or '—'
        bw = it['machine_bandwidth'] or '—'
        if it['status'] == 'done' and it['metrics']:
            mt = json.loads(it['metrics'])
            row = (f"| {name} | {region} | {bw} "
                   f"| {fmt_num(mt.get('loss_pct'))}% | {fmt_num(mt.get('rtt_avg'))} ms "
                   f"| {fmt_num(mt.get('mdev_ms'), 2)} ms "
                   f"| {fmt_num(mt.get('up_mbits'))} Mbit/s | {fmt_num(mt.get('down_mbits'))} Mbit/s ")
            if udp:
                row += (f"| {fmt_num(mt.get('up_udp_loss_pct'), 2)}% / "
                        f"{fmt_num(mt.get('down_udp_loss_pct'), 2)}% "
                        f"| {fmt_num(mt.get('up_jitter_ms'), 3)} / "
                        f"{fmt_num(mt.get('down_jitter_ms'), 3)} ms ")
            else:
                row += f"| {fmt_num(mt.get('retr_total'), 0)} "
            code = mt.get('rating_code') or '?'
            label = mt.get('rating_label') or mt.get('rating') or RATING_CODE_INFO.get(code, '—')
            lines.append(row + f"| **{code} · {label}**：{mt.get('rating_summary', '—')} |")
        else:
            err = (it['error'] or it['status']).replace('|', '\\|').replace('\n', ' ')
            lines.append(f"| {name} | {region} | {bw} | "
                         + ' | '.join(['—'] * (ncols - 4))
                         + f" | **F · 失败**：{err} |")

    lines.append('')
    lines.append('## 诊断详情')
    lines.append('')
    lines.append(f'> {RATING_CONFIG_NOTE}')
    lines.append('')
    for it in items:
        lines.append(f"### {it['machine_name']}")
        lines.append('')
        if it['status'] == 'done' and it['metrics']:
            mt = json.loads(it['metrics'])
            code = mt.get('rating_code') or '?'
            label = mt.get('rating_label') or mt.get('rating') or RATING_CODE_INFO.get(code, '—')
            tags = mt.get('rating_tags') or []
            lines.append(f"- **评级**：{code} · {label}")
            lines.append(f"- **异常标签**：{'、'.join(tags) if tags else '无'}")
            lines.append(f"- **结论**：{mt.get('rating_summary') or '—'}")
            for d in mt.get('rating_diagnostics') or []:
                lines.append(f"- **{d.get('name', '指标')}**（{d.get('status', 'unknown')}）：{d.get('text', '')}")
        else:
            lines.append(f"- **评级**：F · 失败")
            lines.append(f"- **原因**：{it['error'] or it['status']}")
        lines.append('')

    lines.append('')
    lines.append('## 原始数据')
    for idx, it in enumerate(items, 1):
        lines.append('')
        port = int(it.get('port') or run.get('port') or 5201)
        lines.append(
            f"### 后端机器{idx}：{it['machine_name']}"
            f"（{it['machine_region'] or '未标记'} · {it['machine_bandwidth'] or '未标记'} · "
            f"{mask_ip(it['machine_host'])} · 连接端口 {port}）")
        lines.append('')
        lines.append('```')
        if it['status'] == 'done' and it['metrics']:
            mt = json.loads(it['metrics'])
            blocks = [mt.get('ping_block'), mt.get('up_block'), mt.get('down_block')]
            body = '\n\n'.join(b for b in blocks if b)
            lines.append(body if body else '（无输出）')
        else:
            lines.append(f"测试失败：{it['error'] or it['status']}")
        lines.append('```')
    if run.get('error'):
        lines.append('')
        lines.append(f"> ⚠️ {run['error']}")
    return mask_report_text('\n'.join(lines))
