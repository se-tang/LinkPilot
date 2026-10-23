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


def evaluate(mt, machine):
    """按丢包/抖动/重传/收发比/标称带宽达成率给线路打分并写回 mt。"""
    notes = []
    pen = 0

    loss = mt.get('loss_pct')
    if loss is not None:
        if loss == 0:
            notes.append('0%丢包')
        elif loss <= 1:
            pen += 1
            notes.append(f'{fmt_num(loss)}%轻微丢包')
        elif loss <= 3:
            pen += 2
            notes.append(f'{fmt_num(loss)}%丢包偏多')
        else:
            pen += 3
            notes.append(f'{fmt_num(loss)}%严重丢包')

    mdev = mt.get('mdev_ms')
    if mdev is not None:
        if mdev <= 2:
            notes.append(f'抖动{fmt_num(mdev, 2)}ms极小')
        elif mdev <= 5:
            pen += 1
            notes.append(f'抖动{fmt_num(mdev, 2)}ms略大')
        elif mdev <= 15:
            pen += 2
            notes.append(f'抖动{fmt_num(mdev, 2)}ms偏大')
        else:
            pen += 3
            notes.append(f'抖动{fmt_num(mdev, 2)}ms很大')

    retr = mt.get('retr_total')
    if retr is not None:
        if retr == 0:
            notes.append('全程无重传')
        elif retr <= 10:
            pen += 1
            notes.append(f'{retr}次重传')
        elif retr <= 50:
            pen += 2
            notes.append(f'重传{retr}次较多')
        else:
            pen += 3
            notes.append(f'重传{retr}次严重')

    # UDP：以 iperf3 接收端统计的丢包与抖动为准（ping 的 mdev 仍单独参与评价）
    udp_losses = [v for v in (mt.get('up_udp_loss_pct'), mt.get('down_udp_loss_pct')) if v is not None]
    if udp_losses:
        worst = max(udp_losses)
        if worst == 0:
            notes.append('UDP 0%丢包')
        elif worst <= 0.5:
            notes.append(f'UDP 丢包{fmt_num(worst, 2)}%轻微')
        elif worst <= 2:
            pen += 1
            notes.append(f'UDP 丢包{fmt_num(worst, 2)}%偏多')
        else:
            pen += 2
            notes.append(f'UDP 丢包{fmt_num(worst, 2)}%严重')

    jitters = [v for v in (mt.get('up_jitter_ms'), mt.get('down_jitter_ms')) if v is not None]
    if jitters:
        worst_j = max(jitters)
        if worst_j <= 1:
            notes.append(f'iperf3 抖动{fmt_num(worst_j, 2)}ms极小')
        elif worst_j <= 5:
            pen += 1
            notes.append(f'iperf3 抖动{fmt_num(worst_j, 2)}ms略大')
        else:
            pen += 2
            notes.append(f'iperf3 抖动{fmt_num(worst_j, 2)}ms偏大')

    us_, ur_ = mt.get('up_sender_mbits'), mt.get('up_receiver_mbits')
    if us_ and ur_ and ur_ / us_ < 0.9:
        pen += 1 if ur_ / us_ >= 0.7 else 2
        notes.append('上行接收率偏低，传输中有损耗')
    ds_, dr_ = mt.get('down_sender_mbits'), mt.get('down_receiver_mbits')
    if ds_ and dr_ and dr_ / ds_ < 0.9:
        pen += 1 if dr_ / ds_ >= 0.7 else 2
        notes.append('下行接收率偏低，传输中有损耗')

    labeled = parse_bw(machine.get('bandwidth'))
    if labeled:
        for direction, val in (('上行', mt.get('up_mbits')), ('下行', mt.get('down_mbits'))):
            if not val:
                continue
            pct = val / labeled * 100
            if pct < 50:
                pen += 1
                notes.append(f'{direction}仅约标称{labeled:g}M的{pct:.0f}%')
            elif pct < 80:
                pen += 1
                notes.append(f'{direction}低于标称带宽（{fmt_num(val)}/{labeled:g}M）')
            elif pct > 110:
                notes.append(f'{direction}超出标称带宽（{fmt_num(val)}/{labeled:g}M）')

    rating = '优秀' if pen == 0 else '良好' if pen <= 2 else '一般' if pen <= 4 else '较差'
    mt['rating'] = rating
    mt['rating_detail'] = '，'.join(notes) if notes else '—'
    mt['rating_penalty'] = pen
    return mt


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
        lines.append('| 后端机器 | 地区 | 带宽 | ping 丢包 | RTT avg | ping 抖动 | 上行 (sender) '
                     '| 下行 (receiver) | UDP 丢包 上/下 | UDP 抖动 上/下 | 线路质量评价 |')
        lines.append('|---|---|---|---|---|---|---|---|---|---|---|')
    else:
        lines.append('| 后端机器 | 地区 | 带宽 | 丢包率 | RTT avg | 抖动 mdev | 上行 (sender) '
                     '| 下行 (receiver) | 重传 | 线路质量评价 |')
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
            lines.append(row + f"| **{mt.get('rating', '—')}**：{mt.get('rating_detail', '—')} |")
        else:
            err = (it['error'] or it['status']).replace('|', '\\|').replace('\n', ' ')
            lines.append(f"| {name} | {region} | {bw} | "
                         + ' | '.join(['—'] * (ncols - 4))
                         + f" | ❌ 失败：{err} |")

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
