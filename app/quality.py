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
    'throughput_pct': {'warn': 85.0, 'bad': 65.0, 'critical': 40.0},
    'throughput_abs_mbps': {'warn': 300.0, 'bad': 100.0, 'critical': 20.0},
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


def parse_iperf_json(out):
    """解析 `iperf3 -J` 输出 → (sender, receiver, intervals)；不是 JSON 则返回 None。

    面板从 v0.9.0 起让 Agent 带 `-J`，结构化输出比文本正则稳，还能拿到每个
    interval 的吞吐，用来算线路稳定性（CV / P95）。旧 Agent 仍返回文本，
    由 parse_iperf_any 回退。
    """
    text = (out or '').strip()
    start = text.find('{')
    if start < 0:
        return None
    try:
        doc = json.loads(text[start:])
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get('end'), dict):
        return None
    end = doc['end']

    def entry(node, retr=False):
        try:
            bps = float(node.get('bits_per_second'))
        except (TypeError, ValueError):
            return None
        out_entry = {'mbits': bps / 1e6}
        try:
            out_entry['transfer_mb'] = float(node.get('bytes')) / 1048576.0
        except (TypeError, ValueError):
            pass
        if retr and node.get('retransmits') is not None:
            try:
                out_entry['retr'] = int(node['retransmits'])
            except (TypeError, ValueError):
                pass
        return out_entry

    sender = entry(end.get('sum_sent') or {}, retr=True)
    receiver = entry(end.get('sum_received') or {})
    # UDP 的抖动/丢包只有接收侧统计得准，iperf3 放在 end.sum
    udp_sum = end.get('sum') or {}
    if receiver is not None and udp_sum.get('lost_percent') is not None:
        try:
            receiver['jitter_ms'] = float(udp_sum.get('jitter_ms'))
            receiver['lost'] = int(udp_sum.get('lost_packets'))
            receiver['total'] = int(udp_sum.get('packets'))
            receiver['loss_pct'] = float(udp_sum['lost_percent'])
        except (TypeError, ValueError):
            pass
    intervals = []
    for item in doc.get('intervals') or []:
        s = item.get('sum') or {}
        # -O 剔除的那几秒仍会出现在 intervals 里（omitted=true），算稳定性时要跳掉，
        # 否则慢启动的低速样本会把 CV 拉高。
        if s.get('omitted'):
            continue
        try:
            intervals.append(float(s.get('bits_per_second')) / 1e6)
        except (TypeError, ValueError):
            continue
    return sender, receiver, intervals


def parse_iperf_any(out):
    """统一入口：优先按 `-J` JSON 解析，失败则退回文本解析。"""
    parsed = parse_iperf_json(out)
    if parsed:
        return parsed
    sender, receiver = parse_iperf(out)
    return sender, receiver, []


def stability(intervals):
    """区间吞吐的波动：CV（标准差/均值）与 P95。样本不足时返回 None。"""
    vals = [v for v in (intervals or []) if v is not None and v >= 0]
    if len(vals) < 3:
        return None
    mean = sum(vals) / float(len(vals))
    if mean <= 0:
        return None
    var = sum((v - mean) ** 2 for v in vals) / float(len(vals))
    ordered = sorted(vals)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {'samples': len(vals), 'cv_pct': (var ** 0.5) / mean * 100.0,
            'p95_mbits': ordered[idx], 'avg_mbits': mean}


def json_block(sender, receiver, intervals, omit=0):
    """把 JSON 结果压成报告里能看的几行（原始 JSON 太长，不适合塞进报告）。"""
    lines = []
    if sender:
        lines.append(f"[SUM] sender   {fmt_num(sender.get('mbits'))} Mbits/sec"
                     + (f"  retr {int(sender['retr'])}" if sender.get('retr') is not None else ''))
    if receiver:
        extra = ''
        if receiver.get('loss_pct') is not None:
            extra = (f"  jitter {fmt_num(receiver.get('jitter_ms'), 2)} ms"
                     f"  loss {fmt_num(receiver.get('loss_pct'), 2)}%")
        lines.append(f"[SUM] receiver {fmt_num(receiver.get('mbits'))} Mbits/sec{extra}")
    st = stability(intervals)
    if st:
        lines.append(f"# 稳定性：CV {fmt_num(st['cv_pct'], 1)}% · P95 {fmt_num(st['p95_mbits'])} Mbit/s"
                     f"（{st['samples']} 个区间" + (f"，已剔除前 {omit} 秒" if omit else '') + '）')
    return '\n'.join(lines)


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


def parse_metrics(ping_raw, up_raw, down_raw, omit=0):
    """把三段原始输出解析为结构化指标（含报告用的三段摘录块）。

    iperf3 带 `-J` 时输出 JSON：优先按 JSON 解析，并额外给出每个方向的稳定性
    （区间吞吐的 CV 与 P95）；旧 Agent 的文本输出仍走原来的正则解析。
    """
    mt = parse_ping(ping_raw)
    us, ur, up_iv = parse_iperf_any(up_raw)
    ds, dr, down_iv = parse_iperf_any(down_raw)
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
    # 稳定性（只有 -J 才有区间序列；文本输出自然为空，不影响旧记录）
    for tag, iv in (('up', up_iv), ('down', down_iv)):
        st = stability(iv)
        mt[f'{tag}_stability'] = st
        mt[f'{tag}_cv_pct'] = st['cv_pct'] if st else None
        mt[f'{tag}_p95_mbits'] = st['p95_mbits'] if st else None
    mt['omit_seconds'] = int(omit or 0)
    mt['ping_block'] = ping_block(ping_raw)
    mt['up_block'] = (json_block(us, ur, up_iv, omit) if up_iv
                      else summary_block(up_raw))
    mt['down_block'] = (json_block(ds, dr, down_iv, omit) if down_iv
                        else summary_block(down_raw))
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
    """按丢包和上下行吞吐计算 S/A/B/C 评级。

    RTT、mdev、重传只作为展示/诊断信息，不参与等级计算，也不会因缺失而返回数据不足。
    阈值集中在 RATING_THRESHOLDS，属于项目初始经验值，不是行业标准。
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

    def info(name, text, value=None):
        diagnostics.append({'name': name, 'status': 'info', 'text': text, 'value': value})

    up = mt.get('up_mbits')
    down = mt.get('down_mbits')
    if up is None or float(up or 0) <= 0:
        missing.append('上行吞吐')
    if down is None or float(down or 0) <= 0:
        missing.append('下行吞吐')

    loss_values = []
    if mt.get('loss_pct') is not None:
        loss_values.append(float(mt['loss_pct']))
    for key in ('up_udp_loss_pct', 'down_udp_loss_pct'):
        if mt.get(key) is not None:
            loss_values.append(float(mt[key]))
    if not loss_values:
        missing.append('丢包率')
        loss = None
    else:
        loss = max(loss_values)
        sev = _lower_better_severity(loss, RATING_THRESHOLDS['loss_pct'])
        add('丢包率', sev, f'丢包 {fmt_num(loss, 2)}%',
            '存在丢包' if sev != 'ok' else None, loss)

    backend_bw = parse_bw(machine.get('machine_bandwidth') or machine.get('bandwidth'))
    target_bw = parse_bw(run.get('target_bandwidth'))
    known = [v for v in (backend_bw, target_bw) if v and v > 0]
    labeled = min(known) if known else None
    if backend_bw and backend_bw > 0 and target_bw and target_bw > 0:
        baseline_text = (f'后端标称 {backend_bw:g}M、目标标称 {target_bw:g}M，'
                         f'取两端较小值 {labeled:g}M 为瓶颈基准')
    elif labeled:
        side = '后端' if backend_bw and backend_bw > 0 else '目标'
        baseline_text = f'仅{side}标称带宽可用，暂以 {labeled:g}M 为参考，另一端带宽未知'
    else:
        baseline_text = '两端均未标记有效带宽，按绝对吞吐评估'
    for direction, val in (('上行', up), ('下行', down)):
        if val is None or float(val) <= 0:
            continue
        if labeled:
            pct = float(val) / labeled * 100.0
            sev = _higher_better_severity(pct, RATING_THRESHOLDS['throughput_pct'])
            text = f'{direction} {fmt_num(val)} Mbit/s（{baseline_text}），达成率 {pct:.1f}%'
            metric_value = pct
        else:
            sev = _higher_better_severity(
                float(val), RATING_THRESHOLDS['throughput_abs_mbps'])
            text = f'{direction} {fmt_num(val)} Mbit/s（{baseline_text}）'
            metric_value = float(val)
        tag = f'{direction}吞吐偏低' if sev != 'ok' else None
        add(f'{direction}吞吐', sev, text, tag, metric_value)

    rtt = mt.get('rtt_avg')
    mdev = mt.get('mdev_ms')
    retr = mt.get('retr_total')
    info('RTT（仅展示）',
         f'RTT 平均 {fmt_num(rtt)} ms，不参与评级' if rtt is not None else '未提供 RTT，不影响评级')
    info('抖动（仅展示）',
         f'抖动 {fmt_num(mdev, 2)} ms，不参与评级' if mdev is not None else '未提供抖动，不影响评级')
    if not mt.get('udp'):
        info('重传（仅展示）',
             f'重传 {int(retr)} 次，不参与评级' if retr is not None else '未提供重传数据，不影响评级')

    missing = list(dict.fromkeys(missing))
    if missing:
        if '关键指标缺失' not in tags:
            tags.append('关键指标缺失')
        diagnostics.append({
            'name': '数据完整性', 'status': 'critical',
            'text': '缺少关键指标：' + '、'.join(missing), 'value': missing,
        })

    code = '?' if missing else _grade_for_score(score)
    label = RATING_CODE_INFO[code]
    needs_attention = code in ('B', 'C', 'F', '?') or bool(issues)
    if code == '?':
        summary = '数据不足'
        advice = '缺少丢包或上下行结果，暂不能给出可靠等级，请查看诊断并复测。'
    elif code == 'S':
        summary = '表现正常'
        advice = '丢包、上行吞吐和下行吞吐均处于当前经验阈值内。'
    else:
        primary = max(issues, key=lambda x: x['score']) if issues else None
        issue_tags = {item['tag'] for item in issues}
        has_loss = '存在丢包' in issue_tags
        has_up = '上行吞吐偏低' in issue_tags
        has_down = '下行吞吐偏低' in issue_tags
        if has_loss and (has_up or has_down):
            summary = '丢包且吞吐偏低'
        elif has_loss:
            summary = '存在丢包'
        elif has_up and has_down:
            summary = '上下行吞吐偏低'
        elif has_up:
            summary = '上行吞吐偏低'
        elif has_down:
            summary = '下行吞吐偏低'
        else:
            summary = '质量一般'
        advice = _issue_text(primary['tag']) if primary else '查看诊断详情进一步定位。'
    detail = '；'.join(d['text'] for d in diagnostics)
    mt.update({
        'throughput_baseline_mbps': labeled,
        'rating_code': code,
        'rating': label,
        'rating_label': label,
        'rating_tags': tags,
        'rating_summary': summary,
        'rating_advice': advice,
        'rating_detail': detail or '—',
        'rating_diagnostics': diagnostics,
        'rating_penalty': score,
        'needs_attention': needs_attention,
        'rating_threshold_note': RATING_CONFIG_NOTE,
    })
    return mt


def refresh_item_rating(item, run):
    """用开测时的带宽快照重算展示评级；只改返回对象，不回写历史原始数据。"""
    if item.get('status') != 'done' or not item.get('metrics'):
        return item
    try:
        metrics = json.loads(item['metrics'])
    except (TypeError, ValueError):
        return item
    if not isinstance(metrics, dict):
        return item
    evaluate(metrics, item, run)
    item['metrics'] = json.dumps(metrics, ensure_ascii=False)
    item['metrics_obj'] = metrics
    return item


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
        'rating_summary': '测试失败',
        'rating_advice': '未获得可用于线路质量判断的完整结果。',
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


def build_report(run, target, items):
    """生成 Markdown：保留汇总表，并附上每台后端与目标机之间的原始测试输出。"""
    context = dict(run)
    context.setdefault('target_bandwidth', target.get('bandwidth') or '')
    items = [refresh_item_rating(dict(item), context) for item in items]
    udp = bool(int(run.get('udp') or 0))

    def cell(value):
        return str(value if value is not None else '—').replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')

    def with_unit(value, unit, digits=1):
        return '—' if value is None else f'{fmt_num(value, digits)} {unit}'

    lines = [
        '## LinkPilot 测试结果',
        '',
        f"| 机器 | 地区 | 带宽 | 丢包 | RTT | 抖动 | 上行 | 下行 | {'UDP丢包' if udp else '重传'} | 评级 | 说明 |",
        '|---|---|---|---|---|---|---|---|---|---|---|',
    ]
    for it in items:
        name = cell(it['machine_name'])
        region = cell(it['machine_region'] or '—')
        bandwidth = cell(it['machine_bandwidth'] or '—')
        if it['status'] == 'done' and it['metrics']:
            mt = json.loads(it['metrics'])
            loss_values = [v for v in (
                mt.get('loss_pct'), mt.get('up_udp_loss_pct'), mt.get('down_udp_loss_pct')
            ) if v is not None]
            loss = max(loss_values) if loss_values else None
            rtt = mt.get('rtt_avg')
            mdev = mt.get('mdev_ms')
            up = mt.get('up_mbits')
            down = mt.get('down_mbits')
            if udp:
                extra = (f"{fmt_num(mt.get('up_udp_loss_pct'), 2)}% / "
                         f"{fmt_num(mt.get('down_udp_loss_pct'), 2)}%")
            else:
                extra = fmt_num(mt.get('retr_total'), 0)
            code = mt.get('rating_code') or '?'
            label = mt.get('rating_label') or mt.get('rating') or RATING_CODE_INFO.get(code, '—')
            lines.append(
                f"| {name} | {region} | {bandwidth} | {fmt_num(loss, 2)}% "
                f"| {with_unit(rtt, 'ms')} | {with_unit(mdev, 'ms', 2)} "
                f"| {with_unit(up, 'Mbit/s')} | {with_unit(down, 'Mbit/s')} "
                f"| {extra} | **{code} {label}** | {cell(mt.get('rating_summary') or '—')} |")
        else:
            err = cell(it['error'] or it['status'])
            lines.append(
                f"| {name} | {region} | {bandwidth} | — | — | — | — | — | — "
                f"| **F 失败** | {err} |")

    lines.extend(['', '### 原始数据'])
    default_port = int(run.get('port') or 5201)
    for idx, it in enumerate(items, 1):
        port = int(it.get('port') or default_port)
        lines.extend([
            '',
            f"#### 后端机器{idx}：{cell(it['machine_name'])}"
            f"（{cell(it['machine_region'] or '未标记')} · "
            f"{cell(it['machine_bandwidth'] or '未标记')} · "
            f"{mask_ip(it.get('machine_host') or '')} · 连接端口 {port}）",
            '',
            '```',
        ])

        blocks = []
        if it.get('status') == 'done' and it.get('metrics'):
            mt = json.loads(it['metrics'])
            blocks = [mt.get('ping_block'), mt.get('up_block'), mt.get('down_block')]
        if not any(blocks):
            if it.get('status') != 'done':
                blocks.append(f"测试失败：{it.get('error') or it.get('status') or '未知错误'}")
            for label, field in (('Ping', 'ping_raw'), ('上行', 'up_raw'), ('下行', 'down_raw')):
                raw = str(it.get(field) or '').strip()
                if raw:
                    blocks.append(f'{label}原文：\n{raw}')
        body = '\n\n'.join(str(block).strip() for block in blocks if block)
        lines.extend([body or '（无输出）', '```'])

    return mask_report_text('\n'.join(lines))
