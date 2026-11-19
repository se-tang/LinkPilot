"""SQLite 数据层 v2：机器（Agent 令牌接入）、任务队列、面板登录凭据。"""
import datetime
import hashlib
import ipaddress
import json
import os
import random
import secrets
import sqlite3
import string
import threading
import time

from werkzeug.security import generate_password_hash

from . import job_types, quality

_DATA_DIR = os.environ.get('DATA_DIR')
if not _DATA_DIR:
    _DATA_DIR = '/data' if os.name != 'nt' else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
os.makedirs(_DATA_DIR, mode=0o700, exist_ok=True)
try:
    os.chmod(_DATA_DIR, 0o700)
except OSError:
    pass
DB_PATH = os.path.join(_DATA_DIR, 'panel.db')
AUTH_PATH = os.path.join(_DATA_DIR, 'auth.json')
INITIAL_PW_PATH = os.path.join(_DATA_DIR, '.initial_password')
SECRET_PATH = os.path.join(_DATA_DIR, 'secret_key')

# agent 心跳在该秒数内视为在线
ONLINE_WINDOW = 25

_local = threading.local()


def get_db():
    conn = getattr(_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        # WAL 下默认的 synchronous=FULL 会让每次 commit 都 fsync；心跳、任务输出回传
        # 这类小写入很密，改成 NORMAL 后可显著减少落盘次数（掉电最多丢最后几个事务，
        # 不会损坏数据库），这是 SQLite 官方推荐的 WAL 搭配。
        conn.execute('PRAGMA synchronous=NORMAL')
        # 写锁冲突时排队等待，而不是立刻抛 database is locked
        conn.execute('PRAGMA busy_timeout=30000')
        _local.conn = conn
        _secure_data_files()
    return conn


def _secure_data_files():
    for path in (DB_PATH, DB_PATH + '-wal', DB_PATH + '-shm', SECRET_PATH,
                 AUTH_PATH, INITIAL_PW_PATH):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


SCHEMA = '''
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'backend',
    region TEXT NOT NULL DEFAULT '',
    bandwidth TEXT NOT NULL DEFAULT '',
    isp TEXT NOT NULL DEFAULT '',
    lat TEXT NOT NULL DEFAULT '',
    lng TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL DEFAULT '',
    sign_key TEXT NOT NULL DEFAULT '',
    update_key TEXT NOT NULL DEFAULT '',
    agent_hash TEXT NOT NULL DEFAULT '',
    agent_caps TEXT NOT NULL DEFAULT '',
    hostname TEXT NOT NULL DEFAULT '',
    agent_ip TEXT NOT NULL DEFAULT '',
    ip4 TEXT NOT NULL DEFAULT '',
    ip6 TEXT NOT NULL DEFAULT '',
    addr_override TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    probe_error TEXT NOT NULL DEFAULT '',
    probe_error_at TEXT,
    last_seen INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    target_host TEXT NOT NULL DEFAULT '',
    target_region TEXT NOT NULL DEFAULT '',
    target_bandwidth TEXT NOT NULL DEFAULT '',
    ip_version INTEGER NOT NULL DEFAULT 4,
    streams INTEGER NOT NULL DEFAULT 1,
    duration INTEGER NOT NULL DEFAULT 10,
    port INTEGER NOT NULL DEFAULT 5201,
    udp INTEGER NOT NULL DEFAULT 0,
    udp_bandwidth TEXT NOT NULL DEFAULT '100M',
    ping_count INTEGER NOT NULL DEFAULT 200,
    source TEXT NOT NULL DEFAULT 'manual',
    schedule_id INTEGER,
    served_ports TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at TEXT,
    report TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    machine_id INTEGER,
    machine_name TEXT NOT NULL DEFAULT '',
    machine_host TEXT NOT NULL DEFAULT '',
    machine_region TEXT NOT NULL DEFAULT '',
    machine_bandwidth TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    phase TEXT NOT NULL DEFAULT '',
    ping_raw TEXT NOT NULL DEFAULT '',
    up_raw TEXT NOT NULL DEFAULT '',
    down_raw TEXT NOT NULL DEFAULT '',
    metrics TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    timeout INTEGER NOT NULL DEFAULT 120,
    status TEXT NOT NULL DEFAULT 'queued',
    output TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_tombstones (
    token TEXT PRIMARY KEY,
    payload TEXT NOT NULL DEFAULT '',
    sig TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS agent_reg_codes (
    code_hash TEXT PRIMARY KEY,
    machine_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE TABLE IF NOT EXISTS auth_limits (
    key TEXT PRIMARY KEY,
    fails TEXT NOT NULL DEFAULT '[]',
    locked_until REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    target_id INTEGER NOT NULL,
    backend_ids TEXT NOT NULL DEFAULT '[]',
    ip_version INTEGER NOT NULL DEFAULT 4,
    streams INTEGER NOT NULL DEFAULT 1,
    duration INTEGER NOT NULL DEFAULT 10,
    port INTEGER NOT NULL DEFAULT 5201,
    udp INTEGER NOT NULL DEFAULT 0,
    udp_bandwidth TEXT NOT NULL DEFAULT '100M',
    ping_count INTEGER NOT NULL DEFAULT 200,
    ports TEXT NOT NULL DEFAULT '{}',
    interval_seconds INTEGER NOT NULL DEFAULT 3600,
    lifetime_seconds INTEGER NOT NULL DEFAULT 0,
    ends_at TEXT,
    on_busy TEXT NOT NULL DEFAULT 'skip',
    last_run_at TEXT,
    last_run_id INTEGER,
    last_status TEXT NOT NULL DEFAULT '',
    next_run_at TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    skip_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    archived_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS schedule_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL,
    run_id INTEGER,
    planned_at TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    status TEXT NOT NULL DEFAULT 'running',
    note TEXT NOT NULL DEFAULT ''
);
-- 查询路径上的索引：心跳取任务、按轮次取明细、定时任务回填都依赖这几列，
-- 没有索引时每台机器每次心跳都要全表扫 jobs（7 天留存后行数不小）。
CREATE INDEX IF NOT EXISTS idx_jobs_machine_status ON jobs(machine_id, status, id);
CREATE INDEX IF NOT EXISTS idx_run_items_run ON run_items(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_schedule ON runs(schedule_id, id);
CREATE INDEX IF NOT EXISTS idx_schedule_runs_schedule ON schedule_runs(schedule_id, run_id);
-- 面板级开关/配置的通用 KV（面板落点地区、每轮包数等存这里）
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
-- 日常链路监测任务：多任务、双模式，相互独立启停
CREATE TABLE IF NOT EXISTS watch_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'A',
    status TEXT NOT NULL DEFAULT 'running',
    backend_ids TEXT NOT NULL DEFAULT '[]',
    target_ids TEXT NOT NULL DEFAULT '[]',
    interval_seconds INTEGER NOT NULL DEFAULT 10,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT
);
-- 日常链路监测原始样本：Agent 常驻 ping 的每轮结果（rtt 为空 = 该轮全部超时；
-- 丢包率由面板按 sent/recv 计算，不信任 Agent 侧百分比）
CREATE TABLE IF NOT EXISTS ping_watch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL DEFAULT 0,
    machine_id INTEGER NOT NULL,
    dest_id INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'A',
    rtt REAL,
    jit REAL,
    sent INTEGER NOT NULL DEFAULT 0,
    recv INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL
);
-- 分钟聚合：原始样本 2 小时后滚动进这张表，支撑 24 小时 / 7 天趋势
CREATE TABLE IF NOT EXISTS ping_watch_minute (
    task_id INTEGER NOT NULL,
    machine_id INTEGER NOT NULL,
    dest_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    bucket INTEGER NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    recv INTEGER NOT NULL DEFAULT 0,
    rtt_sum REAL NOT NULL DEFAULT 0,
    rtt_min REAL,
    rtt_max REAL,
    samples INTEGER NOT NULL DEFAULT 0,
    timeout INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, machine_id, dest_id, mode, bucket)
);
CREATE INDEX IF NOT EXISTS idx_watch_task_ts ON ping_watch(task_id, machine_id, dest_id, mode, ts);
CREATE INDEX IF NOT EXISTS idx_watch_tasks ON watch_tasks(archived, mode, status);
'''


def init_db():
    db = get_db()
    # 旧版 SSH 凭据表结构 → 直接重建（v2 起不再保存任何 SSH 信息）
    cols = [r['name'] for r in db.execute('PRAGMA table_info(machines)').fetchall()]
    if cols and 'token' not in cols:
        db.executescript(
            'DROP TABLE IF EXISTS run_items; DROP TABLE IF EXISTS jobs; '
            'DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS machines;')
    # v0.11 迁移要先于 SCHEMA：旧库的 ping_watch 需先补列，
    # 否则 SCHEMA 里的新索引会引用不存在的列
    cols_w = [r['name'] for r in db.execute('PRAGMA table_info(ping_watch)').fetchall()]
    if cols_w:
        for col, ddl in (('task_id', 'INTEGER NOT NULL DEFAULT 0'),
                         ('sent', 'INTEGER NOT NULL DEFAULT 0'),
                         ('recv', 'INTEGER NOT NULL DEFAULT 0')):
            if col not in cols_w:
                db.execute(f'ALTER TABLE ping_watch ADD COLUMN {col} {ddl}')
        db.execute('DROP INDEX IF EXISTS idx_watch_machine_ts')
        db.execute('DROP INDEX IF EXISTS idx_watch_task_ts')
    db.executescript(SCHEMA)
    # 旧库补列/补表
    cols_items = [r['name'] for r in db.execute('PRAGMA table_info(run_items)').fetchall()]
    if cols_items and 'phase' not in cols_items:
        db.execute("ALTER TABLE run_items ADD COLUMN phase TEXT NOT NULL DEFAULT ''")
    # 每台后端机可以用各自的 iperf3 端口（0 = 跟随本次测试的默认端口）
    if cols_items and 'port' not in cols_items:
        db.execute('ALTER TABLE run_items ADD COLUMN port INTEGER NOT NULL DEFAULT 0')
    # 目标机在 NAT 后时，其本地监听端口可以与后端机连接端口不同（0 = 跟随默认端口）
    cols_m = [r['name'] for r in db.execute('PRAGMA table_info(machines)').fetchall()]
    if cols_m and 'sign_key' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN sign_key TEXT NOT NULL DEFAULT ''")
    # v2.7：测试地址按协议族分开保存（agent_ip 仍是「最近一次心跳的来源 IP」）
    if cols_m and 'ip4' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN ip4 TEXT NOT NULL DEFAULT ''")
    if cols_m and 'ip6' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN ip6 TEXT NOT NULL DEFAULT ''")
    # 地址探测失败原因（面板侧可见，避免「机器在线却一直无 IPv6」无据可查）
    if cols_m and 'probe_error' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN probe_error TEXT NOT NULL DEFAULT ''")
    if cols_m and 'probe_error_at' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN probe_error_at TEXT")
    # 手动指定测试地址（NAT / 反代后面板观测到的地址不对时兜底；留空 = 自动）
    if cols_m and 'addr_override' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN addr_override TEXT NOT NULL DEFAULT ''")
    # 机器列表顺序由用户拖动决定；旧库按原 id 顺序生成首批位置。
    if cols_m and 'sort_order' not in cols_m:
        db.execute('ALTER TABLE machines ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0')
        db.execute('UPDATE machines SET sort_order=id WHERE sort_order=0')
    # 机器资料补充：运营商标记与可选坐标（无坐标时按地区匹配）
    for col in ('isp', 'lat', 'lng'):
        if cols_m and col not in cols_m:
            db.execute(f"ALTER TABLE machines ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    cols_r = [r['name'] for r in db.execute('PRAGMA table_info(runs)').fetchall()]
    if cols_r and 'ip_version' not in cols_r:
        db.execute('ALTER TABLE runs ADD COLUMN ip_version INTEGER NOT NULL DEFAULT 4')
    # v2.8：测试参数从写死（单线程 / 10 秒 / 5201 / ping 200）改为每次可自定义
    for col, ddl in (('streams', 'INTEGER NOT NULL DEFAULT 1'),
                     ('duration', 'INTEGER NOT NULL DEFAULT 10'),
                     ('port', 'INTEGER NOT NULL DEFAULT 5201'),
                     ('udp', 'INTEGER NOT NULL DEFAULT 0'),
                     ('udp_bandwidth', "TEXT NOT NULL DEFAULT '100M'"),
                     ('ping_count', 'INTEGER NOT NULL DEFAULT 200')):
        if cols_r and col not in cols_r:
            db.execute(f'ALTER TABLE runs ADD COLUMN {col} {ddl}')
    # 注：曾短暂区分过「目标机监听端口 / 后端连接端口」（target_port 列），
    # 但 NAT 商家的端口映射基本都是同号映射，最终只保留一个端口列（port），
    # 旧库残留的 runs.target_port 列不再读取。
    for row in db.execute("SELECT id FROM machines WHERE sign_key=''").fetchall():
        db.execute('UPDATE machines SET sign_key=? WHERE id=?', (secrets.token_hex(32), row['id']))
    # v0.9.0：面板推送 Agent 更新。update_key 只给 root（Agent 账号读不到），
    # agent_hash / agent_caps 由心跳上报，面板据此判断「要不要更新、能不能推」。
    if cols_m and 'update_key' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN update_key TEXT NOT NULL DEFAULT ''")
    if cols_m and 'agent_hash' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN agent_hash TEXT NOT NULL DEFAULT ''")
    if cols_m and 'agent_caps' not in cols_m:
        db.execute("ALTER TABLE machines ADD COLUMN agent_caps TEXT NOT NULL DEFAULT ''")
    for row in db.execute("SELECT id FROM machines WHERE update_key=''").fetchall():
        db.execute('UPDATE machines SET update_key=? WHERE id=?',
                   (secrets.token_hex(32), row['id']))
    # v2.9：测试记录来源（手动 / 定时任务），供记录列表与对比报告区分
    if cols_r and 'source' not in cols_r:
        db.execute("ALTER TABLE runs ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    if cols_r and 'schedule_id' not in cols_r:
        db.execute('ALTER TABLE runs ADD COLUMN schedule_id INTEGER')
    # 本轮在目标机上起过的 server 端口：面板重启后据此回收「孤儿 server」，
    # 否则残留进程会占着端口，让后（尤其是定时任务的下一轮）启动失败
    if cols_r and 'served_ports' not in cols_r:
        db.execute("ALTER TABLE runs ADD COLUMN served_ports TEXT NOT NULL DEFAULT ''")
    # 定时任务生命周期：0 = 不限；ends_at 预先落库，调度查询不用每次换算
    cols_s = [r['name'] for r in db.execute('PRAGMA table_info(schedules)').fetchall()]
    if cols_s and 'lifetime_seconds' not in cols_s:
        db.execute('ALTER TABLE schedules ADD COLUMN lifetime_seconds INTEGER NOT NULL DEFAULT 0')
    if cols_s and 'ends_at' not in cols_s:
        db.execute('ALTER TABLE schedules ADD COLUMN ends_at TEXT')
    if cols_s and 'archived_at' not in cols_s:
        db.execute('ALTER TABLE schedules ADD COLUMN archived_at TEXT')
    cols_jobs = [r['name'] for r in db.execute('PRAGMA table_info(jobs)').fetchall()]
    if cols_jobs and 'kind' not in cols_jobs:
        db.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
    if cols_jobs and 'params' not in cols_jobs:
        db.execute("ALTER TABLE jobs ADD COLUMN params TEXT NOT NULL DEFAULT '{}'")
    # v3.0：停止测试要能真正中止远端命令。面板在任务行上打取消标记，
    # Agent 执行期间的心跳（X-Agent-Busy: 1）会把它带回去并 kill 掉子进程。
    if cols_jobs and 'cancel_requested' not in cols_jobs:
        db.execute("ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
    db.execute("UPDATE jobs SET status='failed', exit_code=-1, "
               "output=output || '[面板] 旧版自由脚本文本任务已作废\\n' "
               "WHERE status IN ('queued','running') AND kind=''")
    cols_tb = [r['name'] for r in db.execute('PRAGMA table_info(agent_tombstones)').fetchall()]
    if cols_tb and 'payload' not in cols_tb:
        # 墓碑是一次性瞬态数据，结构变化直接重建
        db.execute('DROP TABLE agent_tombstones')
        db.executescript(SCHEMA)
    cols_limits = [r['name'] for r in db.execute('PRAGMA table_info(auth_limits)').fetchall()]
    if cols_limits and 'updated_at' not in cols_limits:
        db.execute('ALTER TABLE auth_limits ADD COLUMN updated_at REAL NOT NULL DEFAULT 0')
    db.execute('DELETE FROM auth_limits WHERE updated_at > 0 AND updated_at < ?',
               (time.time() - 7 * 86400,))
    db.commit()
    _secure_data_files()
    repair_incomplete_items()
    mark_stale_runs()
    ensure_auth()


def _throughput_failure(metrics_raw):
    try:
        metrics = json.loads(metrics_raw or '')
    except (TypeError, ValueError):
        return '测试失败：上下行结果缺失或无法解析'
    up = metrics.get('up_mbits')
    down = metrics.get('down_mbits')
    try:
        invalid_up = up is None or float(up) <= 0
        invalid_down = down is None or float(down) <= 0
    except (TypeError, ValueError):
        return '测试失败：上下行结果包含无效数值'
    if invalid_up:
        return '测试失败：未获得有效的上行吞吐结果'
    if invalid_down:
        return '测试失败：未获得有效的下行吞吐结果'
    return ''


def repair_incomplete_items():
    """把历史上只有单边结果的 done 记录收敛为整机失败。

    定时对比的数据必须成对：只有上行或只有下行时，任何均值、趋势和图表都会
    被半个结果污染。这里保留原始输出，清空不可用的 metrics，并把状态改为 failed。
    """
    db = get_db()
    rows = db.execute(
        "SELECT id, run_id, metrics FROM run_items WHERE status='done'").fetchall()
    changed = 0
    affected_runs = set()
    for row in rows:
        reason = _throughput_failure(row['metrics'])
        if not reason:
            continue
        db.execute(
            "UPDATE run_items SET status='failed', error=?, metrics='' WHERE id=?",
            (reason, row['id']))
        affected_runs.add(row['run_id'])
        changed += 1
    for rid in affected_runs:
        counts = db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
            "FROM run_items WHERE run_id=?", (rid,)).fetchone()
        total = int(counts['total'] or 0)
        done = int(counts['done'] or 0)
        status = 'finished' if total and done == total else ('partial' if done else 'failed')
        db.execute('UPDATE runs SET status=? WHERE id=?', (status, rid))
        db.execute('UPDATE schedule_runs SET status=? WHERE run_id=?', (status, rid))
    if changed:
        db.commit()
    return changed


def mark_stale_runs():
    db = get_db()
    db.execute(
        "UPDATE runs SET status='failed', error='面板服务重启导致测试中断', "
        "finished_at=datetime('now','localtime') WHERE status IN ('running','pending')")
    # 重启后没有任何测试应在运行，滞留的 agent 任务一并作废
    db.execute(
        "UPDATE jobs SET status='failed', exit_code=-1, output = output || ?, "
        "finished_at=datetime('now','localtime') WHERE status IN ('queued','running')",
        ('\n[面板] 面板重启，任务作废\n',))
    # 定时任务：把「中断」的那一轮标清楚，但**不动下次执行时间**——
    # 定时任务本身是长期挂着的，面板重启不该让它停掉，恢复后继续按原计划跑。
    # 这里同时把该轮计数写进 schedules（run_count / fail_count），因为轮次记录一旦
    # 变成 interrupted 就没有「待回填」的入口了，不在这一刻记账就会漏掉这一轮。
    db.execute(
        "UPDATE schedule_runs SET status='interrupted', note='面板服务重启导致中断' "
        "WHERE status='running'")
    # 计数从轮次记录重算（每个任务各算各的），天然幂等，重复重启也不会越加越大
    db.execute(
        'UPDATE schedules SET '
        'run_count=(SELECT COUNT(*) FROM schedule_runs sr '
        '           WHERE sr.schedule_id=schedules.id AND sr.run_id IS NOT NULL), '
        'fail_count=(SELECT COUNT(*) FROM schedule_runs sr '
        "            WHERE sr.schedule_id=schedules.id AND sr.run_id IS NOT NULL "
        "            AND sr.status IN ('failed','interrupted','partial')), "
        "last_status=CASE WHEN last_status='running' THEN 'interrupted' ELSE last_status END, "
        "last_error=CASE WHEN last_status='running' THEN '面板服务重启导致本轮中断' "
        '                ELSE last_error END')
    db.commit()


def runs_with_orphan_ports():
    """面板重启时被中断、但目标机上可能还留着 iperf3 -s 的测试记录。

    中断发生在「server 已起、关闭指令还没送出去」的窗口里时，那个裸 server 会一直
    监听到存活上限到点，期间既占着端口（后一轮必然启动失败）又可能被扫到盗刷流量。
    这里把这类记录捞出来，等目标机 Agent 上线后按记录里的端口精确回收。
    """
    rows = get_db().execute(
        "SELECT id, target_id, target_name, served_ports FROM runs "
        "WHERE served_ports != '' AND status='failed' "
        "AND error LIKE '%面板服务重启%' ORDER BY id DESC LIMIT 20").fetchall()
    return [dict(r) for r in rows]


def mark_orphan_ports_cleared(rid):
    update_run(rid, served_ports='')


# ---------------- 登录凭据 ----------------

_USER_ALPHABET = string.ascii_letters + string.digits
_PASS_SPECIALS = "!@#$%^&*()-_=+[]{}?"
_PASS_ALPHABET = string.ascii_letters + string.digits + _PASS_SPECIALS


def _gen_username(n=8):
    return ''.join(secrets.choice(_USER_ALPHABET) for _ in range(n))


def _gen_password(n=16):
    """16 位随机密码：大小写字母 + 数字 + 特殊字符，保证四类至少各一。"""
    for _ in range(100):
        pw = ''.join(secrets.choice(_PASS_ALPHABET) for _ in range(n))
        if (any(c.islower() for c in pw)
                and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in _PASS_SPECIALS for c in pw)):
            return pw
    raise RuntimeError('密码生成失败')


def ensure_auth():
    """首次启动生成随机用户名和密码：auth.json 只存哈希（PANEL_USER/PANEL_PASSWORD
    可覆盖）。初始密码另写 .initial_password（0600）供部署横幅显示一次；
    忘记密码时删除 auth.json 重启即可重新生成。"""
    env_user = os.environ.get('PANEL_USER')
    env_pw = os.environ.get('PANEL_PASSWORD')
    cur = _read_auth_file()
    if cur and cur.get('password_hash') and not env_user and not env_pw:
        return
    user = env_user or (cur or {}).get('user') or _gen_username()
    if env_pw:
        pw = env_pw
    elif cur and cur.get('password'):
        pw = str(cur['password'])      # 旧版明文凭据 → 迁移为哈希
    else:
        pw = _gen_password(16)
    with open(AUTH_PATH, 'w', encoding='utf-8') as f:
        json.dump({'user': user, 'password_hash': generate_password_hash(pw)},
                  f, ensure_ascii=False)
    try:
        os.chmod(AUTH_PATH, 0o600)
    except OSError:
        pass
    if not env_pw:
        with open(INITIAL_PW_PATH, 'w', encoding='utf-8') as f:
            f.write(pw)
        try:
            os.chmod(INITIAL_PW_PATH, 0o600)
        except OSError:
            pass


def _read_auth_file():
    try:
        with open(AUTH_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def get_auth():
    d = _read_auth_file() or {}
    return str(d.get('user') or 'admin'), str(d.get('password_hash') or '')


def get_secret_key():
    if not os.path.exists(SECRET_PATH):
        fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_hex(32))
    else:
        try:
            os.chmod(SECRET_PATH, 0o600)
        except OSError:
            pass
    with open(SECRET_PATH) as f:
        return f.read().strip()


# ---------------- machines ----------------

def machine_online(m):
    if not m:
        return False
    return bool(m.get('last_seen')) and (time.time() - m['last_seen']) <= ONLINE_WINDOW


def active_job_machine_ids():
    """正在执行任务的机器视为在线，避免长时间 iperf/ping 阻塞心跳时误显示离线。"""
    rows = get_db().execute(
        "SELECT DISTINCT machine_id FROM jobs WHERE status='running' "
        "AND started_at IS NOT NULL "
        "AND datetime(started_at, '+' || (timeout + 120) || ' seconds') "
        ">= datetime('now','localtime')").fetchall()
    return {row['machine_id'] for row in rows}


def _row_online(m):
    return machine_online(m)


def _fill_sign_key(m):
    """兼容旧库自愈：机器缺签名密钥时即时补生成（正常情况下 init_db 迁移已补齐）。"""
    if m and not m.get('sign_key'):
        try:
            sk = secrets.token_hex(32)
            get_db().execute('UPDATE machines SET sign_key=? WHERE id=?', (sk, m['id']))
            get_db().commit()
            m['sign_key'] = sk
        except Exception:
            pass
    return m


def get_machines():
    rows = get_db().execute(
        'SELECT * FROM machines ORDER BY sort_order, id').fetchall()
    active_jobs = active_job_machine_ids()
    out = []
    for r in rows:
        m = dict(r)
        m['online'] = machine_online(m) or m['id'] in active_jobs
        m['active_job'] = m['id'] in active_jobs
        _fill_sign_key(m)
        out.append(m)
    return out


def get_machine(mid):
    r = get_db().execute('SELECT * FROM machines WHERE id=?', (mid,)).fetchone()
    if not r:
        return None
    m = dict(r)
    m['active_job'] = m['id'] in active_job_machine_ids()
    m['online'] = machine_online(m) or m['active_job']
    return _fill_sign_key(m)


def get_machine_by_token(token):
    if not token:
        return None
    # 机器数量很小；逐条常量时间比较，避免把令牌比较时序暴露给 Agent API。
    for r in get_db().execute('SELECT * FROM machines').fetchall():
        expected = str(r['token'] or '')
        if expected and secrets.compare_digest(expected, str(token)):
            m = dict(r)
            m['online'] = _row_online(m)
            return m
    return None


def public_machine(m):
    """Return a machine row safe for admin pages and API responses."""
    if not m:
        return m
    out = dict(m)
    out.pop('token', None)
    out.pop('sign_key', None)
    # 更新密钥只留在服务端签名用，绝不能随机器列表下发
    out.pop('update_key', None)
    return out


def create_machine(f):
    db = get_db()
    cur = db.execute(
        'INSERT INTO machines '
        '(name, role, region, bandwidth, isp, lat, lng, addr_override, token, sign_key, update_key, sort_order) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,'
        ' (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM machines))',
        (f['name'], f['role'], f['region'], f['bandwidth'],
         (f.get('isp') or '').strip(), (f.get('lat') or '').strip(), (f.get('lng') or '').strip(),
         (f.get('addr_override') or '').strip(),
         secrets.token_hex(32), secrets.token_hex(32), secrets.token_hex(32)))
    db.commit()
    return get_machine(cur.lastrowid)


def reorder_machines(ids):
    """Persist a complete machine order supplied by the dashboard."""
    db = get_db()
    existing = [r['id'] for r in db.execute(
        'SELECT id FROM machines ORDER BY sort_order, id').fetchall()]
    seen = set()
    order = []
    for raw in ids:
        try:
            mid = int(raw)
        except (TypeError, ValueError):
            continue
        if mid in existing and mid not in seen:
            order.append(mid)
            seen.add(mid)
    # A stale browser may not know about a just-added machine. Keep it instead
    # of dropping it from the list; append unknown rows in their previous order.
    order.extend(mid for mid in existing if mid not in seen)
    db.executemany(
        'UPDATE machines SET sort_order=? WHERE id=?',
        [(index, mid) for index, mid in enumerate(order)])
    db.commit()
    return get_machines()


def update_machine(mid, f):
    db = get_db()
    db.execute('UPDATE machines SET name=?, role=?, region=?, bandwidth=?, isp=?, lat=?, lng=?, addr_override=? '
               'WHERE id=?',
               (f['name'], f['role'], f['region'], f['bandwidth'],
                (f.get('isp') or '').strip(), (f.get('lat') or '').strip(),
                (f.get('lng') or '').strip(), (f.get('addr_override') or '').strip(), mid))
    db.commit()
    return get_machine(mid)


def regen_token(mid):
    db = get_db()
    db.execute('UPDATE machines SET token=?, sign_key=? WHERE id=?',
               (secrets.token_hex(32), secrets.token_hex(32), mid))
    db.commit()
    return get_machine(mid)


_REG_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def _registration_hash(code):
    return hashlib.sha256(str(code or '').strip().upper().encode('utf-8')).hexdigest()


def create_registration_code(mid, ttl_seconds=600):
    """Issue a one-time, short-lived Agent registration code.

    Only a hash is stored, so a database read does not expose a reusable code.
    """
    if not get_machine(mid):
        raise ValueError('机器不存在')
    db = get_db()
    now = time.time()
    db.execute('DELETE FROM agent_reg_codes WHERE expires_at < ?', (now - 86400,))
    db.execute('DELETE FROM agent_reg_codes WHERE machine_id=? AND used_at IS NULL',
               (mid,))
    for _ in range(20):
        code = ''.join(secrets.choice(_REG_ALPHABET) for _ in range(10))
        try:
            db.execute(
                'INSERT INTO agent_reg_codes (code_hash, machine_id, expires_at) '
                'VALUES (?,?,?)',
                (_registration_hash(code), mid, now + max(60, min(int(ttl_seconds), 1800))))
            db.commit()
            return code
        except sqlite3.IntegrityError:
            db.rollback()
    raise RuntimeError('注册码生成失败')


def consume_registration_code(code):
    """Consume a code and rotate the machine credentials atomically."""
    if not code:
        return None
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute(
            'SELECT * FROM agent_reg_codes WHERE code_hash=?',
            (_registration_hash(code),)).fetchone()
        now = time.time()
        if (not row or row['used_at'] is not None
                or float(row['expires_at']) < now):
            db.rollback()
            return None
        db.execute('UPDATE agent_reg_codes SET used_at=? WHERE code_hash=?',
                   (now, row['code_hash']))
        cur = db.execute(
            'UPDATE machines SET token=?, sign_key=?, update_key=?, '
            "agent_hash='', agent_caps='' WHERE id=?",
            (secrets.token_hex(32), secrets.token_hex(32), secrets.token_hex(32),
             row['machine_id']))
        if cur.rowcount != 1:
            db.rollback()
            return None
        db.commit()
    except Exception:
        db.rollback()
        raise
    m = get_machine(row['machine_id'])
    if m:
        m['agent_protocol'] = 2
    return m


def add_registration_failure(mid, reason):
    """Keep a short diagnostic without leaking the submitted registration code."""
    if not mid:
        return
    try:
        set_machine_probe(mid, str(reason)[:200])
    except Exception:
        pass


def auth_limited_seconds(key):
    row = get_db().execute(
        'SELECT locked_until FROM auth_limits WHERE key=?', (key,)).fetchone()
    if not row:
        return 0
    return max(0, int(float(row['locked_until']) - time.time()))


def record_auth_failure(key, window=60, max_fails=5, lock_seconds=300):
    """Persist failure windows so a container restart cannot reset lockout."""
    db = get_db()
    now = time.time()
    with _job_id_lock:
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM auth_limits WHERE key=?', (key,)).fetchone()
            try:
                fails = json.loads(row['fails']) if row else []
            except (TypeError, ValueError):
                fails = []
            fails = [float(t) for t in fails if now - float(t) < window]
            fails.append(now)
            locked_until = float(row['locked_until'] or 0) if row else 0
            if len(fails) >= max_fails:
                locked_until = now + lock_seconds
                fails = []
            db.execute(
                'INSERT INTO auth_limits (key, fails, locked_until, updated_at) '
                'VALUES (?,?,?,?) '
                'ON CONFLICT(key) DO UPDATE SET fails=excluded.fails, '
                'locked_until=excluded.locked_until, updated_at=excluded.updated_at',
                (key, json.dumps(fails), locked_until, now))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return max(0, int(locked_until - now))


def clear_auth_failures(key):
    get_db().execute('DELETE FROM auth_limits WHERE key=?', (key,))
    get_db().commit()


# Cloudflare 官方回源网段（https://www.cloudflare.com/ips/）：
# 绝不能被当作机器的测试地址写入 ip4/ip6。
CF_NETWORKS = [ipaddress.ip_network(n) for n in (
    '173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22',
    '141.101.64.0/18', '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20',
    '197.234.240.0/22', '198.41.128.0/17', '162.158.0.0/15', '104.16.0.0/13',
    '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22',
    '2400:cb00::/32', '2606:4700::/32', '2803:f800::/32', '2405:b500::/32',
    '2405:8100::/32', '2a06:98c0::/29', '2c0f:f248::/32',
)]


def is_cloudflare_ip(ip):
    try:
        a = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    return any(a in n for n in CF_NETWORKS)


def ip_family(ip):
    """IP 协议族：4 / 6；非法地址返回 None。"""
    try:
        return ipaddress.ip_address(str(ip)).version
    except ValueError:
        return None


def machine_test_ip(m, ip_version=4):
    """取该机器用于测试的地址（默认 IPv4）。

    手动指定的测试地址优先——NAT / 反代后面板观测到的地址可能不对（例如前置 CDN
    把入口 IP 当成了机器 IP），这时在机器资料里填死真实地址即可。
    """
    want = 6 if int(ip_version or 4) == 6 else 4
    override = (m.get('addr_override') or '').strip()
    if override and ip_family(override) == want:
        return override
    return (m.get('ip6') if want == 6 else m.get('ip4')) or ''


def touch_machine(mid, hostname, ip):
    """记录心跳：agent_ip 存最近一次来源 IP；同时按协议族写入 ip4/ip6。

    双栈面板下心跳来源可能是 v4 也可能是 v6，按族分别保存后就不会互相覆盖，
    测试地址不会在两族之间跳动。若 ip4/ip6 已有非 Cloudflare 的真实公网地址，
    不会被 Cloudflare 回源边缘 IP 覆盖。
    """
    db = get_db()
    ip = (ip or '')[:64]
    row = db.execute('SELECT ip4, ip6 FROM machines WHERE id=?', (mid,)).fetchone()
    sets = ['last_seen=?', 'hostname=?', 'agent_ip=?']
    args = [int(time.time()), (hostname or '')[:128], ip]
    fam = ip_family(ip)
    if fam == 4:
        cur4 = (row['ip4'] or '') if row else ''
        if not (is_cloudflare_ip(ip) and cur4 and not is_cloudflare_ip(cur4)):
            sets.append('ip4=?')
            args.append(ip)
    elif fam == 6:
        cur6 = (row['ip6'] or '') if row else ''
        if not (is_cloudflare_ip(ip) and cur6 and not is_cloudflare_ip(cur6)):
            sets.append('ip6=?')
            args.append(ip)
    args.append(mid)
    db.execute('UPDATE machines SET ' + ', '.join(sets) + ' WHERE id=?', args)
    db.commit()
    return get_machine(mid)


def set_agent_meta(mid, agent_hash='', agent_caps=None):
    """记录 Agent 自报的脚本摘要与能力（供「可推送更新」判断），无变化则不写库。"""
    mid = int(mid)
    agent_hash = (agent_hash or '')[:64]
    row = get_db().execute(
        'SELECT agent_hash, agent_caps FROM machines WHERE id=?', (mid,)).fetchone()
    if not row:
        return
    sets, args = [], []
    if agent_hash and agent_hash != (row['agent_hash'] or ''):
        sets.append('agent_hash=?')
        args.append(agent_hash)
    if agent_caps is not None and agent_caps[:32] != (row['agent_caps'] or ''):
        sets.append('agent_caps=?')
        args.append(agent_caps[:32])
    if not sets:
        return
    args.append(mid)
    get_db().execute('UPDATE machines SET ' + ', '.join(sets) + ' WHERE id=?', args)
    get_db().commit()


def mark_agent_update_unsupported(mid, reason=''):
    """Agent 明确回报「不认识 update_agent」时收回它的推送能力。

    否则面板会一直显示「可更新」，每次点都白排一次任务；这里清掉 caps，
    界面就会改提示「需要重跑接入命令」。
    """
    get_db().execute(
        "UPDATE machines SET agent_caps='' WHERE id=?", (mid,))
    get_db().commit()


def touch_machine_seen(mid, hostname=''):
    """任务输出回传只刷新在线时间，不从请求来源推断机器测试地址。"""
    sets = ['last_seen=?']
    args = [int(time.time())]
    if hostname:
        sets.append('hostname=?')
        args.append((hostname or '')[:128])
    args.append(mid)
    get_db().execute('UPDATE machines SET ' + ', '.join(sets) + ' WHERE id=?', args)
    get_db().commit()


def mark_agent_incompatible(mid, reason):
    """Stop an old-protocol Agent from appearing online or holding work items."""
    db = get_db()
    db.execute(
        "UPDATE machines SET last_seen=0, probe_error=?, "
        "probe_error_at=datetime('now','localtime') WHERE id=?",
        (str(reason)[:300], mid))
    db.execute(
        "UPDATE jobs SET status='failed', exit_code=-1, "
        "output=output || ?, finished_at=datetime('now','localtime') "
        "WHERE machine_id=? AND status IN ('queued','running')",
        ('\n[面板] Agent 协议版本过旧，任务已取消\n', mid))
    db.commit()


def set_machine_ips(mid, values, force=False):
    """写回面板探测到的地址。默认只补空缺（心跳观测到的地址更可信，不覆盖），
    force=True 用于用户手动「探测地址」时的强制刷新；若已存地址是 Cloudflare
    回源边缘 IP，也允许被探测结果直接覆盖纠正。"""
    db = get_db()
    row = db.execute('SELECT ip4, ip6 FROM machines WHERE id=?', (mid,)).fetchone()
    if not row:
        return None
    sets, args = [], []
    for col in ('ip4', 'ip6'):
        v = str(values.get(col) or '')[:64]
        if not v:
            continue
        if not force and row[col] and not is_cloudflare_ip(row[col]):
            continue
        sets.append(f'{col}=?')
        args.append(v)
    if sets:
        args.append(mid)
        db.execute('UPDATE machines SET ' + ', '.join(sets) + ' WHERE id=?', args)
        db.commit()
    return get_machine(mid)


def set_machine_probe(mid, error=''):
    """记录/清除最近一次地址探测的失败原因（成功时传空串清除）。

    面板据此在机器列表上标出「为什么没有 IPv6」，不必再去翻容器日志或机器上的
    agent.log（例如 Agent 因任务编号回退而拒收探测任务时，这里会写明原因）。
    """
    db = get_db()
    if error:
        db.execute("UPDATE machines SET probe_error=?, "
                   "probe_error_at=datetime('now','localtime') WHERE id=?",
                   (str(error)[:300], mid))
    else:
        db.execute("UPDATE machines SET probe_error='', probe_error_at=NULL WHERE id=?", (mid,))
    db.commit()
    return get_machine(mid)


def delete_machine(mid, payload='', sig=''):
    """删除机器；曾上线过的留下令牌墓碑（内含已签名的卸载任务），
    Agent 下次心跳时自动执行卸载脚本。"""
    db = get_db()
    m = get_machine(mid)
    if not m:
        return
    if m['last_seen']:
        db.execute('INSERT OR REPLACE INTO agent_tombstones (token, payload, sig) VALUES (?,?,?)',
                   (m['token'], payload, sig))
    db.execute('DELETE FROM machines WHERE id=?', (mid,))
    db.execute('DELETE FROM jobs WHERE machine_id=?', (mid,))
    db.execute('DELETE FROM agent_reg_codes WHERE machine_id=?', (mid,))
    db.execute('DELETE FROM ping_watch WHERE machine_id=? OR dest_id=?', (mid, mid))
    db.execute('DELETE FROM ping_watch_minute WHERE machine_id=? OR dest_id=?', (mid, mid))
    db.commit()


def pop_tombstone(token):
    """取出一次性墓碑：命中则删除并返回结构化卸载任务。"""
    if not token:
        return None
    db = get_db()
    r = db.execute('SELECT payload, sig FROM agent_tombstones WHERE token=?',
                   (token,)).fetchone()
    if not r:
        return None
    db.execute('DELETE FROM agent_tombstones WHERE token=?', (token,))
    db.commit()
    return dict(r)


# ---------------- runs ----------------

def _valid_port(v, default=5201):
    try:
        p = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    return p if 1 <= p <= 65535 else default


def _port_or_follow(v):
    """0/空/非法 = 跟随本次测试的默认端口（真实校验在 runner.normalize_params）。"""
    try:
        p = int(str(v).strip())
    except (TypeError, ValueError):
        return 0
    return p if 1 <= p <= 65535 else 0


def create_run(target, backend_ids, ip_version=4, target_host='', source='manual',
               schedule_id=None, **params):
    """新建一次测试记录；params 为本次测试参数（线程/时长/端口/协议/ping 次数）。

    params['port'] 是**目标机端口**：目标机 iperf3 -s 监听它，后端机默认也连它
    （NAT 商家的端口映射一般是同号映射，改这一个值即可）。
    params['ports'] 可选，{机器ID: 端口}：个别后端机单独走不同连接端口的场景。
    source / schedule_id 标记这次测试的来源（手动 or 某个定时任务）。
    """
    db = get_db()
    ip_version = 6 if int(ip_version or 4) == 6 else 4
    default_port = _valid_port(params.get('port') or 5201)
    ports = params.get('ports') or {}
    cur = db.execute(
        'INSERT INTO runs (target_id, target_name, target_host, target_region, target_bandwidth, '
        'ip_version, streams, duration, port, udp, udp_bandwidth, ping_count, source, schedule_id) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (target['id'], target['name'],
         target_host or (target['agent_ip'] or 'IP待agent上报'),
         target['region'], target['bandwidth'], ip_version,
         int(params.get('streams') or 1), int(params.get('duration') or 10),
         default_port, 1 if params.get('udp') else 0,
         str(params.get('udp_bandwidth') or '100M'), int(params.get('ping_count') or 200),
         str(source or 'manual')[:32], schedule_id))
    run_id = cur.lastrowid
    for bid in backend_ids:
        m = get_machine(bid)
        db.execute(
            'INSERT INTO run_items (run_id, machine_id, machine_name, machine_host, '
            'machine_region, machine_bandwidth, port) VALUES (?,?,?,?,?,?,?)',
            (run_id, m['id'], m['name'], m['agent_ip'] or 'IP待agent上报',
             m['region'], m['bandwidth'],
             _port_or_follow(ports.get(bid, ports.get(str(bid))))))
    db.commit()
    return run_id


def get_run(rid):
    r = get_db().execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
    return dict(r) if r else None


def get_runs(limit=50):
    # 列表接口只返回摘要。历史日志/报告可能有上百 KB，逐条塞进列表会让每次
    # 自动刷新传输数 MB 数据，完整内容应由 /api/runs/<id> 按需读取。
    rows = get_db().execute(
        'SELECT id, target_id, target_name, target_host, target_region, target_bandwidth, '
        'ip_version, streams, duration, port, udp, udp_bandwidth, ping_count, '
        'source, schedule_id, status, created_at, finished_at, error '
        'FROM runs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(r) for r in rows]


_RUN_COLS = {'status', 'report', 'log', 'error', 'finished_at', 'served_ports'}


def update_run(rid, **fields):
    cols = [c for c in fields if c in _RUN_COLS]
    if not cols:
        return
    sql = 'UPDATE runs SET ' + ', '.join(f'{c}=?' for c in cols) + ' WHERE id=?'
    get_db().execute(sql, [fields[c] for c in cols] + [rid])
    get_db().commit()


def finish_run(rid, status, report, log_text, error=''):
    update_run(rid, status=status, report=report, log=log_text, error=error,
               finished_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    prune_history()


# 历史保留上限：手动测试与定时任务必须分开。
# 定时任务是长期对比的数据源，用一个 100 条的上限会在第二天就把对比页需要的轮次删掉。
_KEEP_MANUAL_DEFAULT = 100
_KEEP_SCHEDULE_DEFAULT = 3000


def _keep_limit(env_name, default):
    try:
        value = int(str(os.environ.get(env_name) or default).strip())
    except (TypeError, ValueError):
        return default
    return max(1, value)


def prune_history(keep_runs=None):
    """清理过期记录：手动测试与各个定时任务分别保留最近若干轮。

    旧实现对所有 runs 用同一个上限（100 条）做 `DELETE ... WHERE id <= cutoff`，
    定时任务按 5 分钟间隔跑满一天就有 288 轮，对比页和导出报告只能看到最近约
    100 轮，而面板上仍显示「已完成 288 轮」——数据在后台被悄悄删掉了。
    现在按来源分开清理，可用环境变量覆盖：
      PANEL_KEEP_RUNS             手动测试保留条数（默认 100）
      PANEL_KEEP_SCHEDULE_ROUNDS  每个定时任务保留轮次（默认 3000）
    """
    db = get_db()
    keep_manual = (keep_runs if keep_runs is not None
                   else _keep_limit('PANEL_KEEP_RUNS', _KEEP_MANUAL_DEFAULT))
    keep_sched = _keep_limit('PANEL_KEEP_SCHEDULE_ROUNDS', _KEEP_SCHEDULE_DEFAULT)

    def trim(where, args, limit):
        row = db.execute(
            f'SELECT id FROM runs WHERE {where} ORDER BY id DESC LIMIT 1 OFFSET ?',
            (*args, max(0, int(limit) - 1))).fetchone()
        if not row:
            return
        db.execute(
            f'DELETE FROM run_items WHERE run_id IN '
            f'(SELECT id FROM runs WHERE {where} AND id < ?)', (*args, row['id']))
        db.execute(f'DELETE FROM runs WHERE {where} AND id < ?', (*args, row['id']))

    trim('schedule_id IS NULL', (), keep_manual)
    for row in db.execute(
            'SELECT DISTINCT schedule_id FROM runs WHERE schedule_id IS NOT NULL').fetchall():
        trim('schedule_id = ?', (row['schedule_id'],), keep_sched)
    db.execute("DELETE FROM jobs WHERE status IN ('finished','failed') "
               "AND created_at < datetime('now','-7 days')")
    db.execute("DELETE FROM agent_tombstones WHERE created_at < datetime('now','-7 days')")
    db.commit()


def run_counts(rid):
    row = get_db().execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
        'FROM run_items WHERE run_id=?', (rid,)).fetchone()
    return row['total'] or 0, row['done'] or 0, row['failed'] or 0


def run_counts_bulk(run_ids):
    """一次取多轮的 (total, done, failed)，避免列表接口每轮一次查询。"""
    ids = []
    for raw in run_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    out = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ','.join('?' * len(chunk))
        rows = get_db().execute(
            'SELECT run_id, COUNT(*) AS total, '
            "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
            f'FROM run_items WHERE run_id IN ({marks}) GROUP BY run_id', chunk)
        for row in rows:
            out[row['run_id']] = (row['total'] or 0, row['done'] or 0, row['failed'] or 0)
    return out


def delete_run(rid):
    db = get_db()
    db.execute('DELETE FROM run_items WHERE run_id=?', (rid,))
    db.execute('DELETE FROM runs WHERE id=?', (rid,))
    db.commit()


# ---------------- run items ----------------

def get_run_items(rid):
    rows = get_db().execute('SELECT * FROM run_items WHERE run_id=? ORDER BY id', (rid,)).fetchall()
    run = get_run(rid) or {}
    return [quality.refresh_item_rating(dict(r), run) for r in rows]


def get_run_item(iid):
    r = get_db().execute('SELECT * FROM run_items WHERE id=?', (iid,)).fetchone()
    return dict(r) if r else None


_ITEM_COLS = {'status', 'phase', 'ping_raw', 'up_raw', 'down_raw', 'metrics', 'error'}


def update_item(iid, **fields):
    cols = [c for c in fields if c in _ITEM_COLS]
    if not cols:
        return
    sql = 'UPDATE run_items SET ' + ', '.join(f'{c}=?' for c in cols) + ' WHERE id=?'
    get_db().execute(sql, [fields[c] for c in cols] + [iid])
    get_db().commit()


def fail_open_items(rid, error):
    get_db().execute(
        "UPDATE run_items SET status='failed', error=? WHERE run_id=? AND status IN ('pending','running')",
        (error, rid))
    get_db().commit()


# ---------------- agent 任务队列 ----------------

_job_id_lock = threading.Lock()


def _alloc_job_id(db):
    """分配全局严格递增的任务编号（毫秒时间戳打底，永不复用）。

    Agent 端用 last_job_id 做防重放：比历史水位小的任务编号一律拒收。而
    AUTOINCREMENT 在面板数据库被重置后（重新部署 / 删 data 目录）会从 1 重新
    开始，此时每台 Agent 都会把新任务当成重放**静默拒绝**——机器显示在线，
    但地址探测不出来、任务全部超时，且只有机器本地日志能看到原因。
    用时间戳打底后，重置过的面板发号依然大于历史编号，旧 Agent 不重装即可自愈；
    同一库内又始终取「已有最大值 + 1」，因此仍然严格递增。
    """
    row = db.execute('SELECT MAX(id) AS m FROM jobs').fetchone()
    cur_max = int((row['m'] if row else 0) or 0)
    return max(int(time.time() * 1000), cur_max + 1)


def create_job(machine_id, kind, params, timeout):
    """Queue a structured job after validating its type and parameters."""
    clean = job_types.normalize(kind, params)
    params_json = json.dumps(clean, ensure_ascii=False, sort_keys=True)
    db = get_db()
    # 升级旧库时 cmd TEXT NOT NULL 仍然存在；它不再用于执行，但插入必须满足旧约束。
    legacy_cmd = any(row['name'] == 'cmd'
                     for row in db.execute('PRAGMA table_info(jobs)'))
    columns = 'id, machine_id, kind, params, timeout'
    placeholders = '?,?,?,?,?'
    if legacy_cmd:
        columns += ', cmd'
        placeholders += ',?'
    with _job_id_lock:            # 编号分配 + 插入必须原子，否则并发会撞主键
        for _ in range(20):
            jid = _alloc_job_id(db)
            try:
                values = (jid, machine_id, kind, params_json, int(timeout))
                if legacy_cmd:
                    values += ('',)
                db.execute(f'INSERT INTO jobs ({columns}) VALUES ({placeholders})', values)
                db.commit()
                return jid
            except sqlite3.IntegrityError as exc:
                db.rollback()
                # 只有编号主键冲突可重试；NOT NULL/CHECK 等错误必须保留真实原因。
                if (getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY
                        or str(exc) != 'UNIQUE constraint failed: jobs.id'):
                    raise
                time.sleep(0.005)
    raise RuntimeError('任务编号分配失败，请重试')


def get_job(jid):
    r = get_db().execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
    return dict(r) if r else None


def next_queued_job(machine_id):
    """领取最早排队的任务：置为 running 并记录领取时间。"""
    db = get_db()
    r = db.execute(
        "SELECT * FROM jobs WHERE machine_id=? AND status='queued' "
        "AND kind!='' ORDER BY id LIMIT 1",
        (machine_id,)).fetchone()
    if not r:
        return None
    db.execute("UPDATE jobs SET status='running', started_at=datetime('now','localtime') WHERE id=?",
               (r['id'],))
    db.commit()
    return get_job(r['id'])


def _as_job_ids(job_ids):
    ids = []
    for raw in job_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return ids


def running_job_ids_of_run(rid):
    """某轮测试涉及的所有仍在执行的任务（目标机 + 各后端机）。"""
    rows = get_db().execute(
        "SELECT id FROM jobs WHERE status='running' AND machine_id IN ("
        "  SELECT machine_id FROM run_items WHERE run_id=? AND machine_id IS NOT NULL"
        "  UNION SELECT target_id FROM runs WHERE id=?)", (rid, rid)).fetchall()
    return [row['id'] for row in rows]


def request_job_cancel(job_ids):
    """取消任务：排队中的直接作废，执行中的打标记等 Agent 自行中止。

    面板不能直接杀远端进程，但可以让 Agent 在「忙碌心跳」里收到 cancel_job=<id>，
    由 Agent 自己 kill 掉子进程（见 app/agent/install.sh）。
    """
    ids = _as_job_ids(job_ids)
    if not ids:
        return 0
    db = get_db()
    marks = ','.join('?' * len(ids))
    db.execute(
        f"UPDATE jobs SET status='failed', exit_code=-1, output = output || ?, "
        f"finished_at=datetime('now','localtime') "
        f"WHERE status='queued' AND id IN ({marks})",
        ('\n[面板] 任务已取消（尚未开始）\n', *ids))
    cur = db.execute(
        f"UPDATE jobs SET cancel_requested=1 WHERE status='running' AND id IN ({marks})", ids)
    db.commit()
    return cur.rowcount


def job_cancel_requested(jid):
    row = get_db().execute('SELECT cancel_requested FROM jobs WHERE id=?', (jid,)).fetchone()
    return bool(row and row['cancel_requested'])


def running_job_id(machine_id):
    """该机器当前正在执行的任务编号（Agent 同一时间只跑一个）。"""
    row = get_db().execute(
        "SELECT id FROM jobs WHERE machine_id=? AND status='running' ORDER BY id LIMIT 1",
        (machine_id,)).fetchone()
    return row['id'] if row else None


_JOB_OUTPUT_MAX_CHARS = 2_000_000


def append_job_output(jid, text):
    # 输出只保留末尾 2MB，防止异常任务把数据库撑爆（实时日志此前已流式转发）
    get_db().execute(
        'UPDATE jobs SET output = substr(output || ?, -?, ?) WHERE id=?',
        (text, _JOB_OUTPUT_MAX_CHARS, _JOB_OUTPUT_MAX_CHARS, jid))
    get_db().commit()


def finish_job(jid, exit_code):
    db = get_db()
    db.execute(
        "UPDATE jobs SET status=?, exit_code=?, finished_at=datetime('now','localtime') WHERE id=?",
        ('finished' if exit_code == 0 else 'failed', exit_code, jid))
    db.commit()


def fail_job(jid, reason):
    db = get_db()
    db.execute(
        "UPDATE jobs SET status='failed', exit_code=-1, output = output || ?, "
        "finished_at=datetime('now','localtime') WHERE id=?",
        (f'\n[面板] {reason}\n', jid))
    db.commit()


def cancel_queued_jobs(machine_ids):
    """把某些机器仍排队的旧任务作废（新任务开始前清理/停止时）。"""
    if not machine_ids:
        return
    marks = ','.join('?' * len(machine_ids))
    get_db().execute(
        f"UPDATE jobs SET status='failed', exit_code=-1, output = output || ? "
        f'WHERE status=\'queued\' AND machine_id IN ({marks})',
        ('[面板] 任务已取消\n', *machine_ids))
    get_db().commit()


# ---------------- 定时任务（长期重复测试，用于前后端长期对比） ----------------

# 定时任务的字段白名单：改这里就等于改写入面，避免任意列被前端操纵
_SCHED_COLS = {'name', 'enabled', 'target_id', 'backend_ids', 'ip_version', 'streams',
               'duration', 'port', 'udp', 'udp_bandwidth', 'ping_count', 'ports',
               'interval_seconds', 'lifetime_seconds', 'ends_at', 'on_busy',
               'last_run_at', 'last_run_id', 'last_status',
               'next_run_at', 'run_count', 'fail_count', 'skip_count', 'last_error'}


def _sql_now(now=None):
    return now or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def get_schedules(include_archived=False):
    sql = 'SELECT * FROM schedules'
    if not include_archived:
        sql += ' WHERE archived_at IS NULL'
    rows = get_db().execute(sql + ' ORDER BY id').fetchall()
    return [_sched_row(r) for r in rows]


def get_schedule(sid, include_archived=False):
    sql = 'SELECT * FROM schedules WHERE id=?'
    if not include_archived:
        sql += ' AND archived_at IS NULL'
    r = get_db().execute(sql, (sid,)).fetchone()
    return _sched_row(r) if r else None


def _sched_row(r):
    s = dict(r)
    s['backend_ids'] = _load_json(s.get('backend_ids'), [])
    s['ports'] = _load_json(s.get('ports'), {})
    s['enabled'] = bool(s.get('enabled'))
    s['archived'] = bool(s.get('archived_at'))
    return s


def _load_json(raw, default):
    try:
        v = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        v = default
    return v if isinstance(v, type(default)) else default


def create_schedule(fields):
    db = get_db()
    cols = [c for c in fields if c in _SCHED_COLS]
    marks = ','.join('?' * len(cols))
    cur = db.execute(f'INSERT INTO schedules ({",".join(cols)}) VALUES ({marks})',
                     [_sched_value(c, fields[c]) for c in cols])
    db.commit()
    return get_schedule(cur.lastrowid)


def update_schedule(sid, fields):
    db = get_db()
    cols = [c for c in fields if c in _SCHED_COLS]
    if not cols:
        return get_schedule(sid)
    sql = 'UPDATE schedules SET ' + ', '.join(f'{c}=?' for c in cols) + ' WHERE id=?'
    db.execute(sql, [_sched_value(c, fields[c]) for c in cols] + [sid])
    db.commit()
    return get_schedule(sid)


def _sched_value(col, val):
    if col in ('enabled', 'ip_version', 'streams', 'duration', 'port', 'udp',
               'ping_count', 'interval_seconds', 'lifetime_seconds',
               'run_count', 'fail_count', 'skip_count'):
        return int(val or 0)
    if col in ('backend_ids', 'ports'):
        return json.dumps(val if val is not None else ([] if col == 'backend_ids' else {}),
                          ensure_ascii=False)
    return val


def delete_schedule(sid):
    """归档定时任务，保留 schedule_runs 和历史对比所需元数据。"""
    db = get_db()
    db.execute(
        "UPDATE schedules SET archived_at=datetime('now','localtime'), enabled=0, "
        "next_run_at=NULL, last_status='archived', last_error='定时任务已删除，历史记录保留' "
        'WHERE id=? AND archived_at IS NULL', (sid,))
    db.commit()


def set_schedule_enabled(sid, enabled):
    return update_schedule(sid, {'enabled': 1 if enabled else 0})


def due_schedules(now=None):
    """到点且启用中的定时任务（长时间没跑过、面板重启恢复后也会被选中）。"""
    at = _sql_now(now)
    rows = get_db().execute(
        "SELECT * FROM schedules WHERE enabled=1 AND "
        'archived_at IS NULL AND '
        "COALESCE(next_run_at, datetime('now','localtime')) <= ? AND "
        '(ends_at IS NULL OR ends_at > ?) '
        'ORDER BY COALESCE(next_run_at, created_at) ASC', (at, at)).fetchall()
    return [_sched_row(r) for r in rows]


def expire_schedules(now=None):
    """把达到运行时长上限的任务停掉；正在跑的一轮等收尾后再标记。"""
    at = _sql_now(now)
    db = get_db()
    rows = db.execute(
        "SELECT id FROM schedules WHERE enabled=1 AND ends_at IS NOT NULL "
        'AND archived_at IS NULL '
        "AND ends_at <= ? AND COALESCE(last_status, '') != 'running'", (at,)).fetchall()
    ids = [r['id'] for r in rows]
    if ids:
        marks = ','.join('?' * len(ids))
        db.execute(
            f"UPDATE schedules SET enabled=0, last_status='expired', "
            f"last_error='已达到设定的运行时长，任务已自动停止', next_run_at=NULL "
            f"WHERE id IN ({marks})", ids)
        db.commit()
    return ids


def schedule_next_run(sid, next_at, note=None, failed=False, skipped=False):
    """推进下一次执行时间，并把本轮结果计数写回。"""
    fields = {'next_run_at': next_at}
    if skipped:
        fields['skip_count'] = (get_schedule(sid) or {}).get('skip_count', 0) + 1
    elif failed:
        fields['fail_count'] = (get_schedule(sid) or {}).get('fail_count', 0) + 1
    if note is not None:
        fields['last_error'] = note
    update_schedule(sid, fields)
    return get_schedule(sid)


def record_schedule_run(sid, run_id, planned_at, started_at=None, status='running', note=''):
    """写一条定时任务执行记录（run_id 为空表示这一轮没有真正跑起来）。"""
    db = get_db()
    cols = ['schedule_id', 'run_id', 'planned_at', 'status', 'note']
    vals = [sid, run_id, planned_at, status, note]
    if started_at:
        cols.insert(3, 'started_at')
        vals.insert(3, started_at)
    marks = ','.join('?' * len(cols))
    cur = db.execute(f'INSERT INTO schedule_runs ({",".join(cols)}) VALUES ({marks})', vals)
    db.commit()
    return cur.lastrowid


def finish_schedule_run(sid, run_id, status, note=''):
    """测试跑完：回填定时任务与这一轮的状态。

    计数（运行轮数 / 失败轮数）以 schedule_runs 的实际记录为准重算，天然幂等：
    重复回填同一轮不会把计数越加越大，中断的轮次也不会漏计。
    """
    db = get_db()
    row = db.execute(
        'SELECT id FROM schedule_runs WHERE schedule_id=? AND run_id=? ORDER BY id DESC LIMIT 1',
        (sid, run_id)).fetchone()
    if row:
        db.execute('UPDATE schedule_runs SET status=?, note=? WHERE id=?', (status, note, row['id']))
    db.commit()
    counts = db.execute(
        'SELECT COUNT(*) AS n, '
        "SUM(CASE WHEN status IN ('failed','interrupted','partial') THEN 1 ELSE 0 END) AS bad "
        'FROM schedule_runs WHERE schedule_id=? AND run_id IS NOT NULL', (sid,)).fetchone()
    update_schedule(sid, {
        'last_status': status,
        'last_run_at': _sql_now(),
        'last_run_id': run_id,
        'run_count': int(counts['n'] or 0),
        'fail_count': int(counts['bad'] or 0),
        'last_error': '' if status == 'finished' else note,
    })
    return get_schedule(sid)


def get_schedule_runs(sid, limit=200):
    rows = get_db().execute(
        'SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY id DESC LIMIT ?',
        (sid, limit)).fetchall()
    return [dict(r) for r in rows]


def pending_schedule_runs():
    """还没回填结果的定时任务轮次（已启动、但测试还没收尾）。

    用轮次表而不是 schedules.last_run_id：后者在「本轮跳过 / 等待空档」时指向的是
    更早那一轮，用它回填会把计数重复累加。
    """
    rows = get_db().execute(
        'SELECT schedule_id, run_id FROM schedule_runs '
        "WHERE status='running' AND run_id IS NOT NULL ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def schedule_run_window(sid, limit=10000):
    """取该定时任务最近 limit 轮**真正跑起来**的测试记录（旧→新），供对比报告使用。"""
    db = get_db()
    rows = db.execute(
        'SELECT r.* FROM schedule_runs sr JOIN runs r ON r.id = sr.run_id '
        'WHERE sr.schedule_id=? AND sr.run_id IS NOT NULL '
        'ORDER BY sr.id DESC LIMIT ?', (sid, limit)).fetchall()
    if not rows:
        # 兼容旧版本：删除任务时 schedule_runs 已被清掉，只留下 runs.schedule_id。
        rows = db.execute(
            'SELECT * FROM runs WHERE schedule_id=? ORDER BY id DESC LIMIT ?',
            (sid, limit)).fetchall()
    runs = [dict(r) for r in rows][::-1]
    runs_by_id = {run['id']: run for run in runs}
    items_by_run = {run['id']: [] for run in runs}
    ids = list(items_by_run)
    # 长测可能有几千轮，分块取明细，避免 N+1 查询以及 SQLite 变量数上限
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ','.join('?' * len(chunk))
        for row in db.execute(
                f'SELECT * FROM run_items WHERE run_id IN ({marks}) ORDER BY id', chunk):
            it = dict(row)
            quality.refresh_item_rating(it, runs_by_id[it['run_id']])
            try:
                it['metrics_obj'] = json.loads(it['metrics']) if it['metrics'] else None
            except (TypeError, ValueError):
                it['metrics_obj'] = None
            items_by_run[it['run_id']].append(it)
    return [{'run': run, 'items': items_by_run[run['id']]} for run in runs]


# ---------------- 全球视图：日常链路监测（多任务、双模式） ----------------

# 留存策略：原始样本 2 小时（覆盖「最近一次 / 1 分钟均值 / 短窗统计」），
# 分钟聚合 7 天（覆盖 24 小时 / 7 天趋势）。10 秒间隔下单链路原始 ≤1440 行、
# 分钟聚合 ≤10080 行，长期运行不会膨胀。
WATCH_RAW_RETENTION = 2 * 3600
WATCH_MINUTE_RETENTION = 7 * 86400
WATCH_RAW_CAP = 2400               # 单链路原始样本行数上限（防超短间隔刷爆库）
WATCH_STATS_WINDOW = 5 * 60        # 列表「RTT avg / 丢包」的统计窗口
WATCH_QUERY_WINDOW = 30 * 60       # 视图查询窗口：覆盖「数据过期」判定（> 3×间隔即过期）
WATCH_SPARK_N = 30                 # 列表趋势的点数（原始轮次）
WATCH_HISTORY_RANGES = {'1h': 3600, '4h': 4 * 3600, '1d': 86400, '7d': 7 * 86400}
WATCH_TASK_STATUS = ('running', 'paused', 'stopped')


def get_setting(key, default=''):
    row = get_db().execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute('INSERT INTO settings (key, value) VALUES (?,?) '
               'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
    db.commit()


def _watch_int(value, lo, hi, label):
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label}必须是 {lo}–{hi} 的整数')
    if not lo <= v <= hi:
        raise ValueError(f'{label}必须在 {lo}–{hi} 之间')
    return v


def watch_panel_settings():
    """面板级展示设置：模式 B 的面板落点地区、每轮包数。"""
    try:
        count = min(10, max(1, int(get_setting('watch_count', 4))))
    except (TypeError, ValueError):
        count = 4
    return {'panel_region': str(get_setting('watch_panel_region', '') or '')[:32],
            'count': count}


def set_watch_panel_settings(data):
    if 'panel_region' in data:
        set_setting('watch_panel_region', str(data.get('panel_region') or '').strip()[:32])
    if data.get('count') is not None:
        set_setting('watch_count', _watch_int(data.get('count'), 1, 10, '每轮 ping 次数'))
    return watch_panel_settings()


def _task_id_list(value):
    """机器 id 列表：接受数组或逗号/空格分隔的字符串，去重保序。"""
    if isinstance(value, str):
        value = value.replace(',', ' ').split()
    out, seen = [], set()
    for raw in value or []:
        try:
            mid = int(raw)
        except (TypeError, ValueError):
            continue
        if mid > 0 and mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def _watch_task_row(row):
    t = dict(row)
    try:
        t['backend_ids'] = json.loads(t.get('backend_ids') or '[]')
    except ValueError:
        t['backend_ids'] = []
    try:
        t['target_ids'] = json.loads(t.get('target_ids') or '[]')
    except ValueError:
        t['target_ids'] = []
    t['probe_count'] = (len(t['backend_ids']) *
                        (len(t['target_ids']) if t['mode'] == 'A' else 1))
    return t


def watch_tasks_list(include_archived=False):
    sql = 'SELECT * FROM watch_tasks'
    if not include_archived:
        sql += ' WHERE archived=0'
    rows = get_db().execute(sql + ' ORDER BY id').fetchall()
    now = int(time.time())
    out = []
    for row in rows:
        t = _watch_task_row(row)
        last = get_db().execute(
            'SELECT MAX(ts) AS m FROM ping_watch WHERE task_id=?', (t['id'],)).fetchone()
        t['last_report_at'] = last['m'] if last and last['m'] else None
        t['data_age'] = (now - t['last_report_at']) if t['last_report_at'] else None
        out.append(t)
    return out


def watch_task_get(tid):
    row = get_db().execute('SELECT * FROM watch_tasks WHERE id=?', (tid,)).fetchone()
    return _watch_task_row(row) if row else None


def _validate_watch_task(data, current=None):
    name = str(data.get('name', current['name'] if current else '') or '').strip()[:64]
    if not name:
        raise ValueError('任务名称不能为空')
    mode_raw = str(data.get('mode', current['mode'] if current else 'A') or '').strip().upper()
    mode = 'B' if mode_raw == 'B' else 'A'
    backend_ids = (_task_id_list(data['backend_ids']) if 'backend_ids' in data
                   else list(current['backend_ids']))
    if 'target_ids' in data:
        target_ids = _task_id_list(data['target_ids'])
    else:
        target_ids = list(current['target_ids']) if current and mode == current['mode'] else []
    if mode == 'A' and 'target_ids' not in (data or {}) and current is None:
        target_ids = []
    if not backend_ids:
        raise ValueError('请至少选择一台参与探测的机器')
    if mode == 'A' and not target_ids:
        raise ValueError('模式 A 请至少选择一台目标机器')
    if mode == 'B':
        target_ids = []
    interval = _watch_int(data.get('interval', current['interval_seconds'] if current else 10),
                          5, 3600, '探测间隔')
    db = get_db()
    machines = {m['id']: m for m in
                db.execute('SELECT id, role FROM machines').fetchall()}
    for mid in backend_ids:
        if mid not in machines:
            raise ValueError('参与机器不存在或已删除')
    for tid in target_ids:
        if tid not in machines:
            raise ValueError('目标机器不存在或已删除')
        if machines[tid]['role'] != 'target':
            raise ValueError(f'模式 A 的目标必须角色为目标机器（id={tid}）')
    return {'name': name, 'mode': mode, 'backend_ids': backend_ids,
            'target_ids': target_ids, 'interval': interval}


def watch_task_conflicts(mode, backend_ids, target_ids, exclude_id=None):
    """与既有任务重复的探测关系提示（不阻止创建，由用户自行决定）。"""
    conflicts = []
    for t in watch_tasks_list():
        if t['archived'] or t['id'] == exclude_id or t['mode'] != mode:
            continue
        shared = sorted(set(t['backend_ids']) & set(backend_ids))
        if not shared:
            continue
        if mode == 'B':
            conflicts.append({'task': t['name'], 'task_id': t['id'],
                              'machines': shared, 'targets': []})
        else:
            targets = sorted(set(t['target_ids']) & set(target_ids))
            if targets:
                conflicts.append({'task': t['name'], 'task_id': t['id'],
                                  'machines': shared, 'targets': targets})
    return conflicts


def watch_task_create(data):
    v = _validate_watch_task(data)
    db = get_db()
    db.execute(
        'INSERT INTO watch_tasks (name, mode, status, backend_ids, target_ids, interval_seconds) '
        "VALUES (?,?,'running',?,?,?)",
        (v['name'], v['mode'], json.dumps(v['backend_ids']),
         json.dumps(v['target_ids']), v['interval']))
    db.commit()
    tid = db.execute('SELECT MAX(id) AS id FROM watch_tasks').fetchone()['id']
    return watch_task_get(tid), watch_task_conflicts(
        v['mode'], v['backend_ids'], v['target_ids'], exclude_id=tid)


def watch_task_update(tid, data):
    cur = watch_task_get(tid)
    if not cur or cur['archived']:
        raise ValueError('任务不存在或已删除')
    v = _validate_watch_task(data, current=cur)
    db = get_db()
    db.execute(
        'UPDATE watch_tasks SET name=?, mode=?, backend_ids=?, target_ids=?, '
        "interval_seconds=?, updated_at=datetime('now','localtime') WHERE id=?",
        (v['name'], v['mode'], json.dumps(v['backend_ids']),
         json.dumps(v['target_ids']), v['interval'], tid))
    db.commit()
    return watch_task_get(tid), watch_task_conflicts(
        v['mode'], v['backend_ids'], v['target_ids'], exclude_id=tid)


def watch_task_action(tid, action):
    """start=启动/恢复 running；pause=暂停；stop=停止（保留配置与历史）。"""
    cur = watch_task_get(tid)
    if not cur or cur['archived']:
        raise ValueError('任务不存在或已删除')
    status = {'start': 'running', 'resume': 'running', 'pause': 'paused',
              'stop': 'stopped'}.get(str(action).strip().lower())
    if not status:
        raise ValueError(f'不支持的任务操作：{action}')
    db = get_db()
    db.execute("UPDATE watch_tasks SET status=?, updated_at=datetime('now','localtime') "
               'WHERE id=?', (status, tid))
    db.commit()
    return watch_task_get(tid)


def watch_task_delete(tid, purge=False):
    """删除任务：默认只归档配置、保留历史；purge=True 才一并清理样本。"""
    cur = watch_task_get(tid)
    if not cur or cur['archived']:
        raise ValueError('任务不存在或已删除')
    db = get_db()
    db.execute("UPDATE watch_tasks SET archived=1, status='stopped', "
               "updated_at=datetime('now','localtime') WHERE id=?", (tid,))
    if purge:
        db.execute('DELETE FROM ping_watch WHERE task_id=?', (tid,))
        db.execute('DELETE FROM ping_watch_minute WHERE task_id=?', (tid,))
    db.commit()


def agent_watch_plan(machine_id, panel_host=''):
    """某台机器当前应执行的探测清单，供心跳捎带下发。

    返回 [{task_id, mode, interval, dest_id, host}]；模式 B 的 host 由
    调用方（心跳处理器）填面板入口地址。
    """
    plan = []
    addr_cache = None
    for t in watch_tasks_list():
        if t['status'] != 'running' or machine_id not in t['backend_ids']:
            continue
        if t['mode'] == 'B':
            if panel_host:
                plan.append({'task_id': t['id'], 'mode': 'B',
                             'interval': t['interval_seconds'],
                             'dest_id': 0, 'host': panel_host})
            continue
        if addr_cache is None:
            addr_cache = {r['id']: dict(r) for r in get_db().execute(
                'SELECT id, ip4, ip6, addr_override FROM machines').fetchall()}
        for tid in t['target_ids']:
            row = addr_cache.get(tid)
            addr = machine_test_ip(row, 4) or machine_test_ip(row, 6) if row else ''
            if addr:
                plan.append({'task_id': t['id'], 'mode': 'A',
                             'interval': t['interval_seconds'],
                             'dest_id': tid, 'host': addr})
    return plan


def insert_watch_samples(machine_id, text):
    """解析 Agent 上报的监测样本并入库。

    每行格式：W <ts> <task_id> <dest_id> <mode> <rtt_avg|-> <rtt_mdev|->
              <sent> <recv>
    rtt/mdev 为 '-' 表示该轮全部超时；丢包率由面板按 sent/recv 计算，
    不信任 Agent 侧百分比。单次上报最多收 600 行。
    返回入库条数。
    """
    db = get_db()
    now = int(time.time())
    valid_tasks = {t['id']: t for t in watch_tasks_list()}
    rows = []
    for line in str(text or '').splitlines()[:600]:
        parts = line.strip().split()
        if len(parts) != 9 or parts[0] != 'W':
            continue
        try:
            ts = int(parts[1])
            task_id = int(parts[2])
            dest_id = int(parts[3])
            mode = parts[4]
            rtt = None if parts[5] == '-' else float(parts[5])
            jit = None if parts[6] == '-' else float(parts[6])
            sent = int(parts[7])
            recv = int(parts[8])
        except ValueError:
            continue
        task = valid_tasks.get(task_id)
        if not task or task['archived']:
            continue                      # 任务已删除/不存在：样本无处归属，丢弃
        if mode not in ('A', 'B') or dest_id < 0:
            continue
        if mode != task['mode']:
            continue                     # 样本模式与任务模式不符：丢弃，杜绝混线
        if mode == 'B' and dest_id != 0:
            continue
        if not 0 <= sent <= 64 or not 0 <= recv <= sent:
            continue
        if rtt is not None and not 0 < rtt < 60000:
            continue
        # 时钟漂移超过 7 天或明显来自未来的样本没有意义，直接丢
        if ts < now - 7 * 86400 or ts > now + 300:
            continue
        rows.append((task_id, machine_id, dest_id, mode, rtt, jit, sent, recv,
                     min(ts, now)))
    if rows:
        db.executemany(
            'INSERT INTO ping_watch (task_id, machine_id, dest_id, mode, rtt, jit, '
            'sent, recv, ts) VALUES (?,?,?,?,?,?,?,?,?)', rows)
        if random.random() < 0.05:
            rollup_watch_raw()
        db.commit()
    return len(rows)


def rollup_watch_raw():
    """维护线程定期调用：原始样本 → 分钟聚合，并按留存策略裁剪。

    聚合语义：sent/recv/rtt_sum/轮数累加，min/max 取极值；
    每行原始样本只会被聚合一次（同事务内聚合后即删除）。
    """
    db = get_db()
    now = int(time.time())
    cutoff = now - WATCH_RAW_RETENTION
    db.execute(
        '''
        INSERT INTO ping_watch_minute
            (task_id, machine_id, dest_id, mode, bucket, sent, recv, rtt_sum,
             rtt_min, rtt_max, samples, timeout)
        SELECT task_id, machine_id, dest_id, mode, ts - ts % 60,
               SUM(sent), SUM(recv), COALESCE(SUM(rtt), 0),
               MIN(rtt), MAX(rtt),
               SUM(CASE WHEN rtt IS NOT NULL THEN 1 ELSE 0 END),
               SUM(CASE WHEN rtt IS NULL THEN 1 ELSE 0 END)
        FROM ping_watch WHERE ts < ?
        GROUP BY task_id, machine_id, dest_id, mode, ts - ts % 60
        ON CONFLICT(task_id, machine_id, dest_id, mode, bucket) DO UPDATE SET
            sent = sent + excluded.sent,
            recv = recv + excluded.recv,
            rtt_sum = rtt_sum + excluded.rtt_sum,
            rtt_min = CASE WHEN rtt_min IS NULL THEN excluded.rtt_min
                           WHEN excluded.rtt_min IS NULL THEN rtt_min
                           WHEN excluded.rtt_min < rtt_min THEN excluded.rtt_min
                           ELSE rtt_min END,
            rtt_max = CASE WHEN rtt_max IS NULL THEN excluded.rtt_max
                           WHEN excluded.rtt_max IS NULL THEN rtt_max
                           WHEN excluded.rtt_max > rtt_max THEN excluded.rtt_max
                           ELSE rtt_max END,
            samples = samples + excluded.samples,
            timeout = timeout + excluded.timeout
        ''', (cutoff,))
    db.execute('DELETE FROM ping_watch WHERE ts < ?', (cutoff,))
    db.execute('DELETE FROM ping_watch_minute WHERE bucket < ?',
               (now - WATCH_MINUTE_RETENTION,))
    # 单链路原始样本上限（超短间隔保护；正常 2 小时窗口远达不到）
    db.execute(
        'DELETE FROM ping_watch WHERE id IN (SELECT id FROM ('
        ' SELECT id, ROW_NUMBER() OVER (PARTITION BY task_id, machine_id, dest_id, mode'
        '        ORDER BY ts DESC, id DESC) AS rn'
        ' FROM ping_watch) WHERE rn > ?)', (WATCH_RAW_CAP,))
    db.commit()


def _link_status(series, interval, now):
    """链路状态三概念分离：任务状态由任务表决定；这里是「数据」状态。

    - stale  数据过期：最近样本太久（Agent 掉线 / 任务暂停后残留窗口）
    - down   不可达：最近几轮全部丢包
    - warn   网络异常：最新一轮严重丢包（≥ 25%）或近 5 轮累计丢包率 ≥ 15%
             （避免每轮 10 包中偶发丢 1 包或目标机微弱 ICMP 限速导致正常链路频繁闪黄）
    - ok     在线
    """
    if not series:
        return 'stale'
    last_ts = series[-1][0]
    if now - last_ts > max(3 * interval, 120):
        return 'stale'
    recent = series[-5:]
    tail = series[-2:]                      # 末尾连续 2 轮全超时才算不可达（单轮不下结论）
    tail_sent = sum(x[2] for x in tail)
    tail_recv = sum(x[3] for x in tail)
    if tail_sent and tail_recv == 0:
        return 'down'
    last_sent, last_recv = recent[-1][2], recent[-1][3]
    if last_sent and (last_sent - last_recv) / last_sent >= 0.25:
        return 'warn'
    tot_sent = sum(x[2] for x in recent)
    tot_recv = sum(x[3] for x in recent)
    if tot_sent and (tot_sent - tot_recv) / tot_sent >= 0.15:
        return 'warn'
    return 'ok'


def watch_view(mode, task_id=None):
    """全球视图「日常链路」数据：任务列表 + 链路聚合（同一份数据供地图/表格/详情）。

    链路字段：最近 RTT、5 分钟均值/极值/加权丢包、1 分钟均值、抖动、
    状态（ok/warn/down/stale）与最近若干轮序列（供趋势图断点）。
    """
    tasks = [t for t in watch_tasks_list()
             if t['mode'] == mode and (task_id is None or t['id'] == task_id)]
    if not tasks:
        return {'tasks': [], 'links': []}
    task_map = {t['id']: t for t in tasks}
    task_ids = sorted(task_map)
    marks = ','.join('?' * len(task_ids))
    now = int(time.time())
    rows = get_db().execute(
        f'SELECT task_id, machine_id, dest_id, mode, rtt, sent, recv, ts '
        f'FROM ping_watch WHERE task_id IN ({marks}) AND mode = ? AND ts >= ? '
        'ORDER BY ts, id',
        (*task_ids, mode, now - WATCH_QUERY_WINDOW)).fetchall()
    machines = {m['id']: dict(m) for m in get_db().execute(
        'SELECT id, name, region, isp, ip4, ip6, last_seen FROM machines').fetchall()}
    grouped = {}
    for r in rows:
        key = (r['task_id'], r['machine_id'], r['dest_id'])
        g = grouped.setdefault(key, [])
        g.append((r['ts'], r['rtt'], r['sent'], r['recv']))
    links = []
    for (tid, mid, did), series in grouped.items():
        task = task_map[tid]
        machine = machines.get(mid, {})
        dest = machines.get(did, {}) if did else {}
        valid = [x[1] for x in series if x[1] is not None]
        recent5 = series[-5:]
        sent_sum = sum(x[2] for x in recent5)
        recv_sum = sum(x[3] for x in recent5)
        last_ts, last_rtt = series[-1][0], series[-1][1]
        avg1 = [x[1] for x in series if x[1] is not None and x[0] >= now - 60]
        jit = None
        if len(valid) > 1:
            gaps = [abs(b - a) for a, b in zip(valid, valid[1:])]
            jit = sum(gaps) / len(gaps)
        machine_online = bool(machine and machine.get('last_seen')
                              and now - machine['last_seen'] <= ONLINE_WINDOW)
        status = 'stale' if not machine_online and not series[-1][2] else \
            _link_status(series, task['interval_seconds'], now)
        if not machine_online and status == 'ok' and now - last_ts > max(3 * task['interval_seconds'], 120):
            status = 'stale'
        def _display_ip(m_dict):
            if not m_dict:
                return ''
            v4, v6 = m_dict.get('ip4') or '', m_dict.get('ip6') or ''
            if v4 and not is_cloudflare_ip(v4):
                return v4
            if v6 and not is_cloudflare_ip(v6):
                return v6
            return v4 or v6

        links.append({
            'task_id': tid, 'task_name': task['name'],
            'machine_id': mid, 'machine_name': machine.get('name', f'#{mid}'),
            'machine_region': machine.get('region', ''),
            'machine_ip': _display_ip(machine),
            'machine_isp': machine.get('isp') or '',
            'dest_id': did,
            'dest_name': dest.get('name', '') if did else '面板端',
            'dest_region': dest.get('region', '') if did else '',
            'dest_ip': _display_ip(dest) if did else '',
            'rtt': last_rtt,
            'avg': (sum(valid) / len(valid)) if valid else None,
            'avg1m': (sum(avg1) / len(avg1)) if avg1 else None,
            'min': min(valid) if valid else None,
            'max': max(valid) if valid else None,
            'jit': jit,
            'loss': ((1 - recv_sum / sent_sum) * 100) if sent_sum else None,
            'sent': sent_sum, 'recv': recv_sum,
            'status': status,
            'n': len(series), 'last_ts': last_ts,
            'spark': [x[1] for x in series[-WATCH_SPARK_N:]],
        })
    return {'tasks': tasks, 'links': links}


def watch_history(task_id, machine_id, dest_id, mode, rng):
    """单链路历史趋势：1h 用原始样本（分钟桶）；4h/1d/7d 用分钟聚合
    （分别按 5 分钟 / 1 小时 / 2 小时分桶）。"""
    span = WATCH_HISTORY_RANGES.get(rng)
    if not span:
        raise ValueError('不支持的时间范围')
    now = int(time.time())
    points = []
    if rng == '1h':
        rows = get_db().execute(
            'SELECT ts, rtt, sent, recv FROM ping_watch '
            'WHERE task_id=? AND machine_id=? AND dest_id=? AND mode=? AND ts >= ? '
            'ORDER BY ts', (task_id, machine_id, dest_id, mode, now - span)).fetchall()
        buckets = {}
        for r in rows:
            b = r['ts'] - r['ts'] % 60
            agg = buckets.setdefault(b, {'sent': 0, 'recv': 0, 'rtts': []})
            agg['sent'] += r['sent']
            agg['recv'] += r['recv']
            if r['rtt'] is not None:
                agg['rtts'].append(r['rtt'])
        for b in sorted(buckets):
            agg = buckets[b]
            rtt = (sum(agg['rtts']) / len(agg['rtts'])) if agg['rtts'] else None
            loss = ((1 - agg['recv'] / agg['sent']) * 100) if agg['sent'] else None
            points.append({'t': b, 'rtt': rtt, 'loss': loss})
    else:
        step = {'4h': 300, '1d': 3600, '7d': 2 * 3600}[rng]
        rows = get_db().execute(
            'SELECT bucket, sent, recv, samples, rtt_sum FROM ping_watch_minute '
            'WHERE task_id=? AND machine_id=? AND dest_id=? AND mode=? AND bucket >= ? '
            'ORDER BY bucket', (task_id, machine_id, dest_id, mode, now - span)).fetchall()
        buckets = {}
        for r in rows:
            b = r['bucket'] - r['bucket'] % step
            agg = buckets.setdefault(b, {'sent': 0, 'recv': 0, 'rtt_sum': 0.0, 'samples': 0})
            agg['sent'] += r['sent']
            agg['recv'] += r['recv']
            agg['rtt_sum'] += r['rtt_sum']
            agg['samples'] += r['samples']
        for b in sorted(buckets):
            agg = buckets[b]
            rtt = (agg['rtt_sum'] / agg['samples']) if agg['samples'] else None
            loss = ((1 - agg['recv'] / agg['sent']) * 100) if agg['sent'] else None
            points.append({'t': b, 'rtt': rtt, 'loss': loss})
    return {'range': rng, 'points': points}


def watch_test_runs(limit=30):
    """最近若干轮已结束的测试记录摘要，供「测试链路」视图切换轮次。"""
    db = get_db()
    rows = db.execute(
        "SELECT r.id AS run_id, r.target_id, r.target_name, r.target_region, "
        "r.created_at, r.source, r.status, "
        "COALESCE(SUM(CASE WHEN ri.status='done' THEN 1 ELSE 0 END), 0) AS done, "
        "COUNT(ri.id) AS total "
        "FROM runs r LEFT JOIN run_items ri ON ri.run_id = r.id "
        "WHERE r.status IN ('finished','partial') "
        "GROUP BY r.id ORDER BY r.id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def watch_test_links(run_id=None):
    """指定或最近一轮测试（后端 → 目标）的链路数据，供「测试链路」视图。

    另附同一目标机最近 10 轮里各后端的 rtt_avg 序列作趋势。
    """
    db = get_db()
    if run_id is not None:
        run = db.execute(
            "SELECT id, target_id, target_name, target_region, created_at, source, status, ip_version "
            "FROM runs WHERE id=? AND status IN ('finished','partial')",
            (int(run_id),)).fetchone()
    else:
        run = db.execute(
            "SELECT id, target_id, target_name, target_region, created_at, source, status, ip_version "
            "FROM runs WHERE status IN ('finished','partial') ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        return None
    links = []
    for it in db.execute(
            'SELECT machine_id, machine_name, machine_region, status, metrics '
            'FROM run_items WHERE run_id=?', (run['id'],)).fetchall():
        try:
            met = json.loads(it['metrics'] or '{}')
        except ValueError:
            met = {}
        done = it['status'] == 'done'
        links.append({
            'machine_id': it['machine_id'], 'machine_name': it['machine_name'],
            'region': it['machine_region'], 'status': it['status'],
            'rtt': met.get('rtt_avg') if done else None,
            'jit': met.get('mdev_ms') if done else None,
            'loss': met.get('loss_pct') if done else None,
            'up': met.get('up_mbits'), 'down': met.get('down_mbits'),
        })
    trend = {}
    run_ids = [r['id'] for r in db.execute(
        "SELECT id FROM runs WHERE target_id=? AND status IN ('finished','partial') "
        'ORDER BY id DESC LIMIT 10', (run['target_id'],)).fetchall()]
    if run_ids:
        marks = ','.join('?' * len(run_ids))
        for it in db.execute(
                f'SELECT machine_id, metrics FROM run_items WHERE run_id IN ({marks}) '
                'ORDER BY run_id', run_ids).fetchall():
            try:
                met = json.loads(it['metrics'] or '{}')
            except ValueError:
                continue
            rtt = met.get('rtt_avg')
            if rtt is not None:
                trend.setdefault(it['machine_id'], []).append(float(rtt))
    return {
        'run_id': run['id'], 'target_id': run['target_id'],
        'target_name': run['target_name'], 'target_region': run['target_region'],
        'created_at': run['created_at'], 'source': run['source'],
        'status': run['status'], 'links': links, 'trend': trend,
    }
