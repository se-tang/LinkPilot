"""测试结束后的线路告警：按阈值挑出异常机器，POST 到 webhook。

阈值与地址存在 settings 表（面板「异常告警」卡片里可改），缺省值在本文件；
同一台机器在冷却时间内只提醒一次，避免长期任务每 5 分钟刷一次屏。
"""
import json
import threading
import time
import urllib.request

from . import db

DEFAULTS = {
    'alert_enabled': '1',
    'alert_webhook': '',
    'alert_min_loss_pct': '1.0',
    'alert_min_throughput_pct': '40',
    'alert_cooldown_seconds': '600',
}

SETTING_KEYS = tuple(DEFAULTS)


def _num(key):
    raw = db.get_setting(key, DEFAULTS[key])
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def config():
    return {
        'enabled': db.get_setting('alert_enabled', DEFAULTS['alert_enabled']) == '1',
        'webhook': (db.get_setting('alert_webhook', DEFAULTS['alert_webhook']) or '').strip(),
        'min_loss_pct': _num('alert_min_loss_pct'),
        'min_throughput_pct': _num('alert_min_throughput_pct'),
        'cooldown_seconds': int(_num('alert_cooldown_seconds')),
    }


def _metrics(item):
    mt = item.get('metrics_obj')
    if isinstance(mt, dict):
        return mt
    raw = item.get('metrics')
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def evaluate(run, items, cfg=None):
    """返回 [{'machine','reasons','metrics'}]；没有异常返回空列表。"""
    cfg = cfg or config()
    found = []
    for item in items or []:
        mt = _metrics(item)
        reasons = []
        if item.get('status') != 'done':
            reasons.append('测试失败：' + str(item.get('error') or '未知原因')[:120])
        else:
            losses = [mt.get('loss_pct'), mt.get('up_udp_loss_pct'), mt.get('down_udp_loss_pct')]
            losses = [float(v) for v in losses if v is not None]
            if losses:
                worst = max(losses)
                if worst >= cfg['min_loss_pct']:
                    reasons.append(f'丢包 {worst:.2f}%（阈值 {cfg["min_loss_pct"]:g}%）')
            base = mt.get('throughput_baseline_mbps')
            if base:
                for tag, label in (('up', '上行'), ('down', '下行')):
                    value = mt.get(f'{tag}_mbits')
                    if value is None:
                        continue
                    pct = float(value) / float(base) * 100.0
                    if pct < cfg['min_throughput_pct']:
                        reasons.append(
                            f'{label}吞吐 {float(value):.1f} Mbit/s（标称 {float(base):g}M 的 '
                            f'{pct:.0f}%，下限 {cfg["min_throughput_pct"]:g}%）')
        if reasons:
            found.append({
                'machine': item.get('machine_name') or '—',
                'machine_id': item.get('machine_id'),
                'reasons': reasons,
                'metrics': {k: mt.get(k) for k in (
                    'up_mbits', 'down_mbits', 'loss_pct', 'rtt_avg', 'mdev_ms',
                    'retr_total', 'rating_code', 'rating')},
            })
    return found


def build_payload(run, found):
    run = run or {}
    return {
        'type': 'linkpilot.alert',
        'event': 'run_finished',
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'run': {
            'id': run.get('id'),
            'status': run.get('status'),
            'source': run.get('source'),
            'schedule_id': run.get('schedule_id'),
            'target': run.get('target_name'),
            'created_at': run.get('created_at'),
        },
        'alerts': found,
    }


def _post(url, payload):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url, data=data, method='POST',
        headers={'Content-Type': 'application/json', 'User-Agent': 'LinkPilot-Alert/1.0'})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.status


def send_async(url, payload, log=None):
    def worker():
        try:
            status = _post(url, payload)
            msg = f'已发送（HTTP {status}）'
        except Exception as exc:                       # 网络问题不能影响测试收尾
            msg = f'发送失败：{exc}'
        if log:
            try:
                log(f'[告警] {msg}')
            except Exception:
                pass
    threading.Thread(target=worker, daemon=True).start()


def maybe_send(run, items, log=None):
    """测试收尾时调用；返回真正发出的机器数（0 = 没发）。"""
    cfg = config()
    if not cfg['enabled'] or not cfg['webhook']:
        return 0
    found = evaluate(run, items, cfg)
    if not found:
        return 0
    scope = str((run or {}).get('schedule_id') or 'manual')
    now = time.time()
    fresh = []
    for entry in found:
        key = f'alert:{scope}:{entry["machine"]}'
        if now - db.alert_last_sent(key) < cfg['cooldown_seconds']:
            continue
        entry['_key'] = key
        fresh.append(entry)
    if not fresh:
        return 0
    payload = build_payload(run, [{k: v for k, v in e.items() if k != '_key'} for e in fresh])
    send_async(cfg['webhook'], payload, log=log)
    for entry in fresh:
        db.mark_alert_sent(entry['_key'], now)
    return len(fresh)


def send_test():
    """面板「发送测试」按钮：同步发一条，失败直接把原因抛给前端。"""
    cfg = config()
    if not cfg['webhook']:
        raise ValueError('还没有填写 webhook 地址')
    payload = {
        'type': 'linkpilot.alert',
        'event': 'test',
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'message': '这是一条来自 LinkPilot 的测试告警',
    }
    try:
        status = _post(cfg['webhook'], payload)
    except Exception as exc:
        raise RuntimeError(f'发送失败：{exc}')
    return {'ok': True, 'status': status}
