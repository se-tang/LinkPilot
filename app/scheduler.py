"""定时任务调度：按自定义间隔重复跑同一组前后端机器，用于长期对比与参照。

与手动测试的关系：测试执行仍然全部走 runner（同一套参数校验、串行通道、报告生成），
调度器只负责「什么时候开跑」和「这一轮结果记到哪个定时任务名下」。

几条刻意的设计：
  * 面板重启不丢任务：下次执行时间落库（next_run_at），重启后到点继续跑；
    重启时正在跑的那一轮标记为 interrupted，但**不动**下次执行时间。
  * 与手动测试冲突时默认「跳过本轮」，并且把跳过原因写清楚（面板上可见），
    而不是排队等待——长期任务排队会把后面每一轮都推后，对比曲线的时间轴就乱了。
  * 乱序调用 due_schedules 不会重复触发：触发前先把 next_run_at 推到未来。
"""
import datetime
import json
import threading
import time
import traceback

from . import db, quality, runner

# 检查周期：比最短间隔（5 分钟）小得多，保证不会漏掉任何一轮
TICK_SECONDS = 5
MIN_INTERVAL = 300          # 5 分钟
MAX_INTERVAL = 30 * 24 * 3600   # 30 天
MAX_LIFETIME = 365 * 24 * 3600  # 单次长期任务最长运行一年
ROUND_LIMIT = 10000          # 5 分钟间隔跑 30 天约 8640 轮，默认保留完整历史
VIEW_ROUNDS = 200            # 页面图表和明细只取最近若干轮，避免大响应卡住浏览器
_ON_BUSY = ('skip', 'wait')

_started = False
_start_lock = threading.Lock()


class InvalidSchedule(ValueError):
    """定时任务字段不合法（继承 ValueError，Web 层统一转成 400）。"""


def now_str(ts=None):
    return datetime.datetime.fromtimestamp(
        ts if ts is not None else time.time()).strftime('%Y-%m-%d %H:%M:%S')


def parse_time(text):
    """把库里的 'YYYY-mm-dd HH:MM:SS' 解析成时间戳；解析不了返回 None。"""
    if not text:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return time.mktime(time.strptime(str(text)[:19], fmt))
        except (TypeError, ValueError):
            continue
    return None


def normalize_schedule(data, base=None):
    """校验并收敛定时任务字段；非法值直接报错，不静默改动用户输入。"""
    base = base or {}

    def pick(key, default):
        return data[key] if key in data else base.get(key, default)

    def num(key, lo, hi, label, default):
        raw = pick(key, default)
        if raw is None or str(raw).strip() == '':
            return default
        try:
            v = int(str(raw).strip())
        except (TypeError, ValueError):
            raise InvalidSchedule(f'{label}必须是整数（收到 {raw!r}）')
        if not lo <= v <= hi:
            raise InvalidSchedule(f'{label}需在 {lo}–{hi} 之间（收到 {v}）')
        return v

    name = str(pick('name', '') or '').strip()[:64] or '定时任务'
    try:
        target_id = int(str(pick('target_id', 0)).strip())
    except (TypeError, ValueError):
        raise InvalidSchedule('请选择目标机器')
    if not target_id:
        raise InvalidSchedule('请选择目标机器')
    backends = []
    for x in (pick('backend_ids', []) or []):
        try:
            v = int(str(x).strip())
        except (TypeError, ValueError):
            raise InvalidSchedule(f'后端机器编号必须是整数（收到 {x!r}）')
        if v and v not in backends:
            backends.append(v)
    if not backends:
        raise InvalidSchedule('请至少选择一台后端机器')

    ip_version = 6 if str(pick('ip_version', 4)).strip() == '6' else 4
    interval = num('interval_seconds', MIN_INTERVAL, MAX_INTERVAL, '间隔时间（秒）',
                   base.get('interval_seconds') or 3600)
    lifetime = num('lifetime_seconds', 0, MAX_LIFETIME, '任务运行时长（秒）',
                   base.get('lifetime_seconds') or 0)
    on_busy = str(pick('on_busy', 'skip') or 'skip').strip()
    if on_busy not in _ON_BUSY:
        on_busy = 'skip'
    ports = {}
    raw_ports = pick('ports', {}) or {}
    if isinstance(raw_ports, dict):
        for k, v in raw_ports.items():
            try:
                mid = int(str(k).strip())
                p = int(str(v).strip())
            except (TypeError, ValueError):
                continue
            if 1 <= p <= 65535:
                ports[mid] = p
    enabled = pick('enabled', True)
    enabled = 0 if str(enabled) in ('0', 'False', 'false', '') else 1
    return {
        'name': name,
        'enabled': enabled,
        'target_id': target_id,
        'backend_ids': backends,
        'ip_version': ip_version,
        'streams': num('streams', 1, 32, '线程数', 1),
        'duration': num('duration', 1, 300, '单向测试时长（秒）', 10),
        'port': num('port', 1, 65535, '目标机端口', 5201),
        'ping_count': num('ping_count', 1, 2000, 'ping 次数', 200),
        'udp': 1 if pick('udp', 0) else 0,
        'udp_bandwidth': str(pick('udp_bandwidth', '100M') or '100M').strip().upper(),
        'ports': ports,
        'interval_seconds': interval,
        'lifetime_seconds': lifetime,
        'on_busy': on_busy,
    }


def _validate_refs(s):
    """触发前检查机器是否还在、角色是否还对——机器被删/改角色时给出可读的原因。"""
    target = db.get_machine(s['target_id'])
    if not target:
        return None, [], '目标机器已被删除'
    if target['role'] != 'target':
        return None, [], f"机器「{target['name']}」的角色已不是「目标机器」"
    backends = []
    for bid in s['backend_ids']:
        m = db.get_machine(bid)
        if not m:
            return None, [], f'后端机器 #{bid} 已被删除'
        if m['role'] != 'backend':
            return None, [], f"机器「{m['name']}」的角色已不是「后端机器」"
        backends.append(m)
    return target, backends, ''


def schedule_summary(s):
    """面板列表用：定时任务 + 人类可读的下次时间与参数摘要。"""
    out = dict(s)
    expired = is_expired(s)
    archived = bool(s.get('archived'))
    out['expired'] = expired and not archived
    out['archived'] = archived
    nxt = s.get('next_run_at')
    out['next_run_in'] = ''
    ts = parse_time(nxt)
    if s.get('enabled') and not expired and not archived and ts:
        delta = int(ts - time.time())
        out['next_run_in'] = _human_delta(delta)
    out['interval_text'] = _human_duration(int(s.get('interval_seconds') or 0))
    lifetime = int(s.get('lifetime_seconds') or 0)
    out['lifetime_text'] = _human_duration(lifetime) if lifetime else '不限'
    if archived:
        out['interval_text'] = '历史归档'
        out['lifetime_text'] = '不再执行'
    end_ts = parse_time(s.get('ends_at'))
    out['end_in'] = _human_delta(int(end_ts - time.time())) if end_ts and not expired else ''
    out['backend_count'] = len(s.get('backend_ids') or [])
    tgt = db.get_machine(s['target_id'])
    out['target_name'] = ((tgt or {}).get('name') or s.get('target_name')
                          or '（已删除）')
    out['target_host'] = (tgt or {}).get('agent_ip') or s.get('target_host') or ''
    names = []
    for idx, bid in enumerate(s.get('backend_ids') or []):
        m = db.get_machine(bid)
        names.append((m or {}).get('name') or (
            (s.get('backend_names') or [None] * len(s.get('backend_ids') or []))[idx]
            if idx < len(s.get('backend_ids') or []) else None
        ) or f'#{bid}(已删除)')
    out['backend_names'] = names
    return out


def _human_duration(sec):
    """人话化的间隔文案。数字与单位之间用不换行空格，面板窄列里不会被拆行。"""
    sec = int(sec or 0)
    if sec <= 0:
        return '—'
    if sec % 86400 == 0:
        return f'{sec // 86400}\u00a0天'
    if sec % 3600 == 0:
        return f'{sec // 3600}\u00a0小时'
    if sec % 60 == 0:
        return f'{sec // 60}\u00a0分钟'
    return f'{sec}\u00a0秒'


def _human_delta(sec):
    if sec <= 0:
        return '即将执行'
    if sec < 60:
        return f'{sec} 秒后'
    if sec < 3600:
        return f'{sec // 60} 分钟后'
    if sec < 86400:
        return f'{sec // 3600} 小时 {sec % 3600 // 60} 分后'
    return f'{sec // 86400} 天 {sec % 86400 // 3600} 小时后'


# ---------------- 触发与推进 ----------------

def compute_next(interval_seconds, frm=None):
    """下一次执行时间 = 现在 + 间隔（固定间隔语义，避免延误后越跑越偏）。"""
    frm = frm if frm is not None else time.time()
    return now_str(frm + max(MIN_INTERVAL, int(interval_seconds or MIN_INTERVAL)))


def compute_end(lifetime_seconds, frm=None):
    """运行时长从保存设置时起算；0 表示不限。"""
    lifetime = int(lifetime_seconds or 0)
    if lifetime <= 0:
        return None
    frm = frm if frm is not None else time.time()
    return now_str(frm + lifetime)


def is_expired(s, at=None):
    end = parse_time((s or {}).get('ends_at'))
    return bool(end and end <= (at if at is not None else time.time()))


def next_run_from(interval_seconds, frm=None):
    """由于「本轮被跳过 / 冲突」而重新排队时，给一个更短的复查间隔。"""
    frm = frm if frm is not None else time.time()
    return now_str(frm + max(60, min(int(interval_seconds or 0) or 60, 300)))


def trigger(sid, planned_at=None, manual=False):
    """触发一轮：返回 (ok, note)。冲突/校验失败都写回定时任务，面板可见。"""
    s = db.get_schedule(sid)
    if not s:
        return False, '定时任务不存在'
    if is_expired(s):
        return False, '定时任务已达到设定的运行时长，请先编辑并延长运行时长'
    if not s.get('enabled') and not manual:
        return False, '定时任务已停用'
    planned_at = planned_at or now_str()
    target, backends, err = _validate_refs(s)
    if err:
        db.record_schedule_run(sid, None, planned_at, status='skipped', note=err)
        db.update_schedule(sid, {'last_status': 'skipped', 'last_error': err,
                                 'last_run_at': now_str(),
                                 'skip_count': int(s.get('skip_count') or 0) + 1})
        db.update_schedule(sid, {'next_run_at': compute_next(s['interval_seconds'])})
        return False, err

    params = {
        'streams': s['streams'], 'duration': s['duration'], 'port': s['port'],
        'udp': bool(s['udp']), 'udp_bandwidth': s['udp_bandwidth'],
        'ping_count': s['ping_count'], 'ports': s.get('ports') or {},
    }
    try:
        run_id = runner.start_run(
            s['target_id'], s['backend_ids'], s['ip_version'], params=params,
            source='schedule', schedule_id=s['id'], label=f"定时任务「{s['name']}」")
    except (RuntimeError, ValueError) as e:
        note = str(e)
        busy = '已有测试任务正在运行' in note
        if busy and s.get('on_busy') == 'wait':
            # 选择「等待」：不记跳过，1–5 分钟后复查（同一个计划周期内不会漏掉这一轮）
            db.update_schedule(sid, {'next_run_at': next_run_from(s['interval_seconds']),
                                     'last_status': 'waiting',
                                     'last_error': '面板有测试在跑，本轮等待中'})
            return False, '等待中：' + note
        db.record_schedule_run(sid, None, planned_at, status='skipped', note=note)
        db.update_schedule(sid, {
            'last_status': 'skipped', 'last_error': note, 'last_run_at': now_str(),
            'skip_count': int(s.get('skip_count') or 0) + 1,
            'next_run_at': compute_next(s['interval_seconds']),
        })
        return False, note

    db.record_schedule_run(sid, run_id, planned_at, status='running',
                           note='手动立即执行' if manual else '')
    db.update_schedule(sid, {'last_status': 'running', 'last_run_id': run_id,
                             'last_run_at': now_str(), 'last_error': '',
                             'next_run_at': compute_next(s['interval_seconds'])})
    return True, f'已启动测试 #{run_id}'


def due_check_once():
    """跑一次到点检查（测试里直接调用，避免起线程）。"""
    now = now_str()
    db.expire_schedules(now)
    for s in db.due_schedules(now):
        try:
            trigger(s['id'], planned_at=now)
        except Exception:
            traceback.print_exc()


# ---------------- 完成回填 ----------------

def finish_pending():
    """扫描「已启动但还没回填」的定时任务轮次，把结果写回。

    只认那些「已经安排了、但还没收尾」的轮次记录（schedule_runs.status='running'），
    而不是看 schedules.last_run_id —— 后者在「跳过本轮 / 等待空档」时可能是更早的
    那一轮，用它会导致计数重复累加。用轮询而不是回调：runner 不需要知道调度器的
    存在，面板重启后也能自愈。
    """
    for entry in db.pending_schedule_runs():
        sid, rid = entry['schedule_id'], entry['run_id']
        run = db.get_run(rid)
        if not run:
            db.finish_schedule_run(sid, rid, 'failed', '测试记录已不存在')
            continue
        if run['status'] in ('running', 'pending'):
            continue
        _total, done, failed = db.run_counts(rid)
        note = run.get('error') or ''
        # 面板重启导致的中断：api_status() 会把 running 归一成 failed，这里要还原成
        # 「已中断」并计入失败，否则面板上会出现「完成 N 轮却一次都没跑成」的错账
        interrupted = '面板服务重启' in note
        status = 'interrupted' if interrupted else run['status']
        if not note and failed:
            note = f'{failed} 台后端失败'
        if not note and done == 0:
            note = '本轮全部失败'
        db.finish_schedule_run(sid, rid, status, note)
        cur = db.get_schedule(sid)
        if cur and not cur.get('next_run_at'):
            db.update_schedule(sid, {'next_run_at': compute_next(cur['interval_seconds'])})


def _loop():
    while True:
        try:
            finish_pending()
            due_check_once()
        except Exception:
            traceback.print_exc()
        time.sleep(TICK_SECONDS)


def start_scheduler():
    """启动后台调度线程（幂等，重复调用只生效一次）。"""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, daemon=True).start()


# ---------------- 长期对比报告 ----------------

def _agg(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {'n': len(vals), 'avg': sum(vals) / len(vals), 'min': min(vals), 'max': max(vals)}


def _md_cell(value):
    return str(value if value is not None else '').replace('\\', '\\\\').replace(
        '|', '\\|').replace('\r', ' ').replace('\n', ' ')


def _throughput_failure(mt):
    up = (mt or {}).get('up_mbits')
    down = (mt or {}).get('down_mbits')
    try:
        invalid_up = up is None or float(up) <= 0
        invalid_down = down is None or float(down) <= 0
    except (TypeError, ValueError):
        return '测试失败：吞吐结果包含无效数值'
    if invalid_up:
        return '上行测试失败：未获得有效吞吐结果'
    if invalid_down:
        return '下行测试失败：未获得有效吞吐结果'
    return ''


RATING_RANK = {
    'S': 5, 'A': 4, 'B': 3, 'C': 2, 'F': 1, '?': 0,
    '优秀': 5, '良好': 4, '一般': 3, '较差': 2, '失败': 1, '数据不足': 0,
}


def _rating_code(value):
    return {
        '优秀': 'S', '良好': 'A', '一般': 'B', '较差': 'C',
        '失败': 'F', '数据不足': '?',
    }.get(value, value)


def _snapshot_schedule(sid, window):
    if not window:
        return None
    latest = window[-1]
    run = latest['run']
    items = latest['items']
    backend_ids, backend_names = [], []
    for item in items:
        if item.get('machine_id') not in backend_ids:
            backend_ids.append(item.get('machine_id'))
            backend_names.append(item.get('machine_name') or f"#{item.get('machine_id')}")
    return {
        'id': sid,
        'name': f'已删除的定时任务 #{sid}',
        'enabled': False,
        'archived': True,
        'archived_at': run.get('finished_at') or run.get('created_at'),
        'target_id': run.get('target_id'),
        'target_name': run.get('target_name') or '（已删除）',
        'target_host': run.get('target_host') or '',
        'backend_ids': backend_ids,
        'backend_names': backend_names,
        'ip_version': int(run.get('ip_version') or 4),
        'streams': int(run.get('streams') or 1),
        'duration': int(run.get('duration') or 10),
        'port': int(run.get('port') or 5201),
        'udp': int(run.get('udp') or 0),
        'udp_bandwidth': run.get('udp_bandwidth') or '100M',
        'ping_count': int(run.get('ping_count') or 200),
        'interval_seconds': 0,
        'lifetime_seconds': 0,
        'ends_at': None,
        'run_count': len(window),
        'fail_count': sum(1 for entry in window
                          if entry['run'].get('status') in ('failed', 'partial', 'interrupted')),
        'skip_count': 0,
        'last_status': latest['run'].get('status') or '',
        'last_error': '',
        'last_run_at': run.get('created_at') or '',
        'next_run_at': None,
    }


def comparison_data(sid, limit=ROUND_LIMIT, view_limit=None):
    """把该定时任务的历史轮次整理成「按机器 × 按轮次」的对比数据。"""
    s = db.get_schedule(sid, include_archived=True)
    window = db.schedule_run_window(sid, limit=limit)
    if not s:
        s = _snapshot_schedule(sid, window)
    if not s:
        return None
    machines = []
    index = {}
    rounds = []
    for entry in window:
        run = entry['run']
        rounds.append({
            'run_id': run['id'],
            'created_at': run['created_at'],
            'finished_at': run.get('finished_at') or '',
            'status': run['status'],
            'target_host': run.get('target_host') or '',
            'done': sum(1 for it in entry['items']
                        if it['status'] == 'done'
                        and not _throughput_failure(it.get('metrics_obj'))),
            'total': len(entry['items']),
        })
        for it in entry['items']:
            key = it['machine_id'] if it['machine_id'] else f"name:{it['machine_name']}"
            if key not in index:
                index[key] = {
                    'key': key, 'machine_id': it['machine_id'],
                    'name': it['machine_name'], 'region': it['machine_region'],
                    'bandwidth': it['machine_bandwidth'],
                    'series': [], 'up': [], 'down': [], 'loss': [], 'rtt': [],
                    'mdev': [], 'retr': [], 'udp_loss': [], 'rating': [],
                }
                machines.append(index[key])
            m = index[key]
            mt = it.get('metrics_obj') or {}
            failure = _throughput_failure(mt) if it['status'] == 'done' else ''
            effective_status = 'failed' if failure else it['status']
            m['series'].append({
                'run_id': run['id'], 'at': run['created_at'], 'status': effective_status,
                'up': mt.get('up_mbits'), 'down': mt.get('down_mbits'),
                'loss': mt.get('loss_pct'), 'rtt': mt.get('rtt_avg'),
                'mdev': mt.get('mdev_ms'), 'retr': mt.get('retr_total'),
                'udp_loss': mt.get('up_udp_loss_pct'),
                'rating': _rating_code(mt.get('rating_code') or mt.get('rating')),
                'rating_tags': mt.get('rating_tags') or [],
                'rating_summary': mt.get('rating_summary') or '',
                'error': it.get('error') or failure,
            })
            if effective_status != 'done':
                continue
            m['up'].append(mt.get('up_mbits'))
            m['down'].append(mt.get('down_mbits'))
            m['loss'].append(mt.get('loss_pct'))
            m['rtt'].append(mt.get('rtt_avg'))
            m['mdev'].append(mt.get('mdev_ms'))
            m['retr'].append(mt.get('retr_total'))
            m['udp_loss'].append(mt.get('up_udp_loss_pct'))
            rating = _rating_code(mt.get('rating_code') or mt.get('rating'))
            if rating:
                m['rating'].append(rating)

    stats = []
    for m in machines:
        ups, downs = m['up'], m['down']
        trend = None
        trend_reason = None
        # 趋势用「后半段均值 vs 前半段均值」比较：比首末单点更抗抖动
        if len(ups) >= 4:
            half = len(ups) // 2
            a, b = _agg(ups[:half]), _agg(ups[half:])
            if a and b and a['avg']:
                trend = (b['avg'] - a['avg']) / a['avg'] * 100.0
            else:
                trend_reason = '前半段均值为 0，无法计算'
        else:
            trend_reason = f'有效样本不足 4 轮（当前 {len(ups)} 轮）'
        stats.append({
            'name': m['name'], 'region': m['region'], 'bandwidth': m['bandwidth'],
            'samples': len(ups),
            'up': _agg(ups), 'down': _agg(downs),
            'loss': _agg(m['loss']), 'rtt': _agg(m['rtt']),
            'mdev': _agg(m['mdev']), 'retr': _agg(m['retr']),
            'udp_loss': _agg(m['udp_loss']),
            'trend_pct': trend,
            'trend_reason': trend_reason,
            'best_rating': max(m['rating'], key=lambda r: RATING_RANK.get(r, -1)) if m['rating'] else None,
            'worst_rating': min(m['rating'], key=lambda r: RATING_RANK.get(r, -1)) if m['rating'] else None,
        })

    total_rounds = len(rounds)
    view_rounds = rounds
    machines_out = machines
    if view_limit and view_limit > 0:
        view_rounds = rounds[-view_limit:]
        visible_ids = {r['run_id'] for r in view_rounds}
        machines_out = [
            dict(m, series=[p for p in m['series'] if p['run_id'] in visible_ids])
            for m in machines
        ]

    return {
        'schedule': schedule_summary(s),
        'rounds': view_rounds,
        'machines': [{'key': str(m['key']), 'name': m['name'], 'region': m['region'],
                      'bandwidth': m['bandwidth'], 'series': m['series']} for m in machines_out],
        'stats': stats,
        'total_rounds': total_rounds,
        'view_rounds': len(view_rounds),
    }


def build_comparison_report(sid, limit=ROUND_LIMIT, view_limit=VIEW_ROUNDS):
    """长期对比 Markdown：概览、线路表现、上下行矩阵和异常记录。"""
    data = comparison_data(sid, limit=limit, view_limit=view_limit)
    if not data:
        return ''
    s = data['schedule']
    rounds = data['rounds']
    lines = ['# iperf3 长期对比报告', '']
    state = ('历史归档' if s.get('archived') else
             '已到期' if s.get('expired') else
             '启用中' if s['enabled'] else '已停用')
    proto = 'IPv6' if int(s['ip_version']) == 6 else 'IPv4'
    kind = f"UDP（-b {s['udp_bandwidth']}/流）" if s['udp'] else 'TCP'
    finished = max(0, int(s['run_count']) - int(s['fail_count']))
    lines += [
        f"> **{s['name']}** · {state} · {s['target_name']} ← {s['backend_count']} 台后端",
        '',
        '## 测试概览',
        '',
        '| 项目 | 详情 |',
        '|---|---|',
        f"| 目标机器 | {s['target_name']}（{quality.mask_ip(s.get('target_host') or '')}） |",
        f"| 后端线路 | {'、'.join(s['backend_names']) or '—'} |",
        f"| 测试参数 | {proto} · {kind} · -P {s['streams']} · 上/下行各 {s['duration']} 秒 · 端口 {s['port']} · ping {s['ping_count']} 次 |",
        f"| 执行周期 | 每 {s['interval_text']} |",
        f"| 运行时长 | {s['lifetime_text']}"
        + (f" · 计划结束 {s['ends_at']}" if s.get('ends_at') else ' · 不会自动停止') + ' |',
        f"| 完成情况 | **已完成轮次：{int(s['run_count'])} 轮**（成功 {finished} / 失败 {s['fail_count']} / 未跑成 {s['skip_count']}） |",
        f"| 当前状态 | {_md_cell(s.get('last_status') or '待首次执行')}"
        + (f" · {_md_cell(s['last_error'])}" if s.get('last_error') else '') + ' |',
    ]
    if rounds:
        lines.append(
            f"| 数据范围 | 最近 {len(rounds)}/{data['total_rounds']} 轮 · "
            f"{rounds[0]['created_at']} ~ {rounds[-1]['created_at']} |")
    lines.append('')

    if not rounds:
        lines.append('> 还没有完成任何一轮测试，稍后再来看。')
        return quality.mask_report_text('\n'.join(lines))

    series_len = {}
    for m in data['machines']:
        series_len[m['name']] = len(m['series'])
    lines += [
        '## 线路表现',
        '',
        '| 线路 | 成功率 | 上行均值 / 区间 (Mbit/s) | 下行均值 / 区间 (Mbit/s) '
        '| 丢包 / RTT | 趋势 | 评价 |',
        '|---|---:|---|---|---|---:|---|',
    ]
    for st in data['stats']:
        up, down, loss = st['up'], st['down'], st['loss']
        rtt = st['rtt']
        trend = '—'
        if st['trend_pct'] is not None:
            t = st['trend_pct']
            arrow = '↑' if t > 3 else ('↓' if t < -3 else '→')
            trend = f'{arrow} {t:+.1f}%'
        elif st.get('trend_reason'):
            trend = f"—（{st['trend_reason']}）"
        ratings = ' / '.join(x for x in (st['best_rating'], st['worst_rating']) if x) or '—'
        total = max(1, series_len.get(st['name'], st['samples']))
        success = st['samples'] / total * 100
        up_text = '—'
        if up:
            up_text = (f"{quality.fmt_num(up['avg'])} / "
                       f"{quality.fmt_num(up['min'])}–{quality.fmt_num(up['max'])}")
        down_text = '—'
        if down:
            down_text = (f"{quality.fmt_num(down['avg'])} / "
                         f"{quality.fmt_num(down['min'])}–{quality.fmt_num(down['max'])}")
        quality_text = '—'
        if loss:
            quality_text = quality.fmt_num(loss['avg'], 2) + '%'
            if rtt:
                quality_text += ' / ' + quality.fmt_num(rtt['avg']) + ' ms'
        lines.append(
            f"| {st['name']} | {success:.0f}% ({st['samples']}/{total}) | {up_text} "
            f"| {down_text} | {quality_text} | {trend} | {ratings} |")
    lines.append('')

    by_round = {}
    for m in data['machines']:
        for pt in m['series']:
            by_round.setdefault(pt['run_id'], {})[m['name']] = pt
    machine_names = [m['name'] for m in data['machines']]
    for direction, title in (('up', '上行'), ('down', '下行')):
        lines += [
            f'## {title}吞吐矩阵（Mbit/s）',
            '',
            '| 时间 | 轮次 | ' + ' | '.join(machine_names) + ' |',
            '|---|---:|' + '---:|' * len(machine_names),
        ]
        for r in rounds:
            cells = []
            points = by_round.get(r['run_id'], {})
            for name in machine_names:
                pt = points.get(name)
                if not pt or pt['status'] != 'done':
                    cells.append('失败')
                else:
                    cells.append(quality.fmt_num(pt[direction]))
            lines.append(f"| {r['created_at'][5:16]} | #{r['run_id']} | " + ' | '.join(cells) + ' |')
        lines.append('')

    failures = []
    for r in rounds:
        for name, pt in by_round.get(r['run_id'], {}).items():
            if pt['status'] != 'done':
                failures.append(
                    f"| {r['created_at']} | #{r['run_id']} | {name} | "
                    f"{_md_cell(pt['error'] or '测试失败')} |")
    lines += ['## 异常记录', '']
    if failures:
        lines += [
            '| 时间 | 轮次 | 后端线路 | 原因 |',
            '|---|---:|---|---|',
            *failures,
        ]
    else:
        lines.append('本轮数据范围内没有失败记录。')
    lines += [
        '',
        '> 趋势为后半段上行均值相对前半段的涨跌，±3% 内视为稳定。'
        '矩阵中的“失败”表示该机器上下行未同时取得有效结果，不计入均值与趋势。',
    ]
    return quality.mask_report_text('\n'.join(lines))
