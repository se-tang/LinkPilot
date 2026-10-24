"""LinkPilot 面板 Web 服务：登录验证、机器管理（Agent 接入）、测试任务、Agent API。"""
import base64
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, job_types, quality, runner, scheduler

# 版本号：这是唯一权威（面板页面、部署横幅、install.sh 都从/沿用它取）。
# 还在测试阶段，因此用 0.x 版本号。
APP_VERSION = '0.7.20'

app = Flask(__name__)
app.json.ensure_ascii = False
app.secret_key = db.get_secret_key()
app.permanent_session_lifetime = datetime.timedelta(days=7)

# 安全配置：请求体上限 + 会话 Cookie 属性
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get('PANEL_COOKIE_SECURE') == '1':
    # 套了 HTTPS 反代时设置，Cookie 只经加密连接传输
    app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_NAME'] = 'linkpilot_session'

AGENT_INTERNAL_PORT = int(os.environ.get('PANEL_AGENT_INTERNAL_PORT', '8001'))
PANEL_HTTPS = os.environ.get('PANEL_HTTPS') == '1'
ALLOW_INSECURE_AGENT = os.environ.get('PANEL_ALLOW_INSECURE_AGENT') == '1'
PROXY_SECRET = os.environ.get('PANEL_PROXY_SECRET', '')
TRUST_LOCAL_PROXY = os.environ.get('PANEL_TRUST_LOCAL_PROXY') == '1'

AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent')

# 无需登录即可访问：登录页/登录接口/健康检查/Agent 脚本与 Agent API（用令牌认证）
_PUBLIC_PREFIXES = ('/agent/', '/api/agent/')
_PUBLIC_PATHS = {'/login', '/api/login', '/api/health', '/favicon.ico'}

# 登录防爆破：同一 IP 60 秒内失败 5 次 → 锁定 300 秒（SQLite 持久化）
_FAIL_WINDOW = 60.0
_FAIL_MAX = 5
_LOCK_SECONDS = 300


# Cloudflare 官方回源网段（https://www.cloudflare.com/ips/，如官方调整请同步更新）
_CF_NETWORKS = [ipaddress.ip_network(n) for n in (
    '173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22',
    '141.101.64.0/18', '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20',
    '197.234.240.0/22', '198.41.128.0/17', '162.158.0.0/15', '104.16.0.0/13',
    '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22',
    '2400:cb00::/32', '2606:4700::/32', '2803:f800::/32', '2405:b500::/32',
    '2405:8100::/32', '2a06:98c0::/29', '2c0f:f248::/32',
)]


def _is_cloudflare_ip(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in _CF_NETWORKS)


def _proxy_headers_trusted():
    supplied = request.headers.get('X-LinkPilot-Proxy-Secret', '')
    if PROXY_SECRET and secrets.compare_digest(str(supplied), str(PROXY_SECRET)):
        return True
    if not TRUST_LOCAL_PROXY:
        return False
    try:
        if int(request.environ.get('SERVER_PORT', '0')) == AGENT_INTERNAL_PORT:
            return False
        remote = ipaddress.ip_address(request.remote_addr or '')
    except ValueError:
        return False
    return remote.is_loopback or remote.is_private


def _client_ip():
    """Return the client address only when the forwarding trust chain is proven.

    Caddy injects a per-install proxy secret. X-Forwarded-* is ignored on direct
    requests, so a client cannot forge agent_ip by supplying its own headers.
    A direct Cloudflare request is the only non-proxy case where CF-Connecting-IP
    is trusted, and only because the TCP peer itself is a Cloudflare edge.
    """
    ra = request.remote_addr or ''
    proxy_ok = _proxy_headers_trusted()
    cf_header = ''
    if proxy_ok or _is_cloudflare_ip(ra):
        candidate = request.headers.get('CF-Connecting-IP', '').strip()
        try:
            cf_header = ipaddress.ip_address(candidate).compressed if candidate else ''
        except ValueError:
            cf_header = ''
    if _is_cloudflare_ip(ra) and cf_header:
        return cf_header[:64]
    if proxy_ok:
        if cf_header:
            return cf_header[:64]
        xff = request.headers.get('X-Forwarded-For', '')
        if xff:
            last = xff.split(',')[-1].strip()
            try:
                return ipaddress.ip_address(last).compressed[:64]
            except ValueError:
                pass
    try:
        return ipaddress.ip_address(ra).compressed[:64]
    except ValueError:
        return ra[:64]


def _login_locked_seconds(ip):
    return db.auth_limited_seconds(f'login:{ip}')


def _record_login_fail(ip):
    return db.record_auth_failure(
        f'login:{ip}', window=_FAIL_WINDOW, max_fails=_FAIL_MAX,
        lock_seconds=_LOCK_SECONDS)


def _auth_tag():
    _, password_hash = db.get_auth()
    return hashlib.sha256(password_hash.encode('utf-8')).hexdigest()[:24] if password_hash else ''


def _session_user():
    user = session.get('user')
    if not user or not secrets.compare_digest(str(session.get('auth_tag') or ''), _auth_tag()):
        return ''
    return str(user)


@app.before_request
def init_request_security():
    g.csp_nonce = secrets.token_urlsafe(18)
    return None


@app.after_request
def security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    resp.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    nonce = getattr(g, 'csp_nonce', '')
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "manifest-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    forwarded_https = (_proxy_headers_trusted()
                       and request.headers.get('X-Forwarded-Proto', '').lower() == 'https')
    if PANEL_HTTPS or forwarded_https:
        resp.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    if resp.mimetype == 'text/html' or request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


def _on_agent_port():
    try:
        return int(request.environ.get('SERVER_PORT', '0')) == AGENT_INTERNAL_PORT
    except (TypeError, ValueError):
        return False


def _is_agent_path(path):
    return path.startswith('/api/agent/') or path.startswith('/agent/')


@app.before_request
def agent_port_guard():
    """The public Agent listener can never be used to reach the management UI."""
    if _on_agent_port() and request.path != '/api/health' and not _is_agent_path(request.path):
        return jsonify({'error': 'not found'}), 404
    return None


@app.before_request
def csrf_guard():
    """CSRF 防线：浏览器发起的跨站写请求（Origin 与站点不符）直接拒绝。
    Agent 接口用令牌认证，不经过 Cookie，不在限制范围内。"""
    if request.method not in ('POST', 'PUT', 'DELETE'):
        return None
    p = request.path
    if not p.startswith('/api/') or p.startswith('/api/agent/'):
        return None
    origin = request.headers.get('Origin')
    if origin and urlsplit(origin).netloc != request.host:
        return jsonify({'error': '已拒绝跨站请求'}), 403
    return None


def _job_sig(sign_key, job_id, payload_b64):
    """HMAC over the base64-encoded structured payload."""
    if not sign_key:
        return ''
    return hmac.new(sign_key.encode('utf-8'),
                    f'{job_id}:{payload_b64}'.encode('utf-8'),
                    hashlib.sha256).hexdigest()


@app.before_request
def auth_gate():
    p = request.path
    if (p in _PUBLIC_PATHS or p in ('/api/agent/register',)
            or p.startswith(_PUBLIC_PREFIXES) or p.startswith('/static/')):
        return None
    if not _session_user():
        if p == '/':
            return redirect('/login')
        if p.startswith('/api/'):
            return jsonify({'error': '未登录或登录已过期'}), 401
        return redirect('/login')
    return None


@app.route('/')
def index():
    agent_url = os.environ.get('PANEL_AGENT_URL', '').rstrip('/')
    if not agent_url:
        agent_url = request.url_root.rstrip('/')
    return render_template(
        'index.html', version=APP_VERSION, agent_url=agent_url,
        allow_insecure_agent=ALLOW_INSECURE_AGENT, csp_nonce=g.csp_nonce)


@app.route('/login')
def login_page():
    if _session_user():
        return redirect('/')
    return render_template('login.html', version=APP_VERSION, csp_nonce=g.csp_nonce)


@app.post('/api/login')
def api_login():
    ip = _client_ip() or '?'
    locked = _login_locked_seconds(ip)
    if locked:
        return jsonify({'error': f'失败次数过多，已锁定，请 {locked} 秒后再试'}), 429
    data = request.get_json(force=True, silent=True) or {}
    user, pw_hash = db.get_auth()
    ok = (secrets.compare_digest(str(data.get('user') or ''), user)
          and bool(pw_hash)
          and check_password_hash(pw_hash, str(data.get('password') or '')))
    if not ok:
        _record_login_fail(ip)
        return jsonify({'error': '用户名或密码错误'}), 401
    db.clear_auth_failures(f'login:{ip}')
    session.clear()
    session['user'] = user
    session['auth_tag'] = _auth_tag()
    session.permanent = True
    return jsonify({'ok': True})


@app.post('/api/logout')
def api_logout():
    session.clear()
    return jsonify({'ok': True})


@app.get('/api/me')
def api_me():
    return jsonify({'user': _session_user()})


@app.get('/api/health')
def api_health():
    return jsonify({'ok': True})


# ---------------- 机器管理 ----------------

@app.get('/api/machines')
def api_machines():
    return jsonify([db.public_machine(m) for m in db.get_machines()])


@app.get('/api/machines/<int:mid>')
def api_machine_get(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    return jsonify(db.public_machine(m))


def _machine_fields(data):
    def clean(k, maxlen, default=''):
        v = data.get(k)
        s = default if v is None else str(v)
        # 过滤竖线/换行，避免破坏 Markdown 报告表格；并限制长度
        s = re.sub(r'[|\r\n\t]+', ' ', s).strip()
        return s[:maxlen]

    name = clean('name', 64)
    if not name:
        raise ValueError('名称不能为空')
    role = clean('role', 16, 'backend')
    if role not in ('backend', 'target'):
        raise ValueError('角色无效')
    # 手动测试地址：留空 = 自动（心跳观测 / 本机探测）；填了就按它测
    addr = clean('addr_override', 64)
    if addr:
        try:
            ipaddress.ip_address(addr)
        except ValueError:
            raise ValueError('手动测试地址不是合法 IP（留空则自动探测）')
    return {'name': name, 'role': role,
            'region': clean('region', 32), 'bandwidth': clean('bandwidth', 32),
            'addr_override': addr}


@app.post('/api/machines')
def api_machine_create():
    data = request.get_json(force=True, silent=True) or {}
    fields = _machine_fields(data)
    return jsonify(db.public_machine(db.create_machine(fields)))


@app.put('/api/machines/<int:mid>')
def api_machine_update(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    data = request.get_json(force=True, silent=True) or {}
    fields = _machine_fields(data)
    return jsonify(db.public_machine(db.update_machine(mid, fields)))


@app.put('/api/machines/order')
def api_machine_order():
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get('ids')
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': '机器顺序不能为空'}), 400
    if len(ids) > 500:
        return jsonify({'error': '机器数量超出限制'}), 400
    machines = db.reorder_machines(ids)
    return jsonify({
        'ok': True,
        'machines': [db.public_machine(m) for m in machines],
    })


@app.post('/api/machines/<int:mid>/registration-code')
def api_machine_registration_code(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    code = db.create_registration_code(mid, ttl_seconds=600)
    agent_url = os.environ.get('PANEL_AGENT_URL', '').rstrip('/') or request.url_root.rstrip('/')
    return jsonify({
        'machine_id': mid,
        'code': code,
        'agent_url': agent_url,
        'expires_in': 600,
    })


@app.delete('/api/machines/<int:mid>')
def api_machine_delete(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    payload = sig = ''
    if m['last_seen'] and m['sign_key']:
        payload = job_types.encode('uninstall')
        sig = _job_sig(m['sign_key'], 'tombstone-uninstall', payload)
    db.delete_machine(mid, payload, sig)
    return jsonify({'ok': True})


@app.post('/api/machines/<int:mid>/discover')
def api_machine_discover(mid):
    """手动触发地址探测（异步，几秒后刷新机器列表即可看到结果）。"""
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    if not db.machine_online(m):
        return jsonify({'error': '该机器 Agent 未上线，无法探测地址'}), 400
    runner.queue_discovery(mid, force=True)
    return jsonify({'ok': True})


# ---------------- Agent 脚本下发（公开，令牌在命令参数里） ----------------

def _serve_agent_file(name):
    path = os.path.join(AGENT_DIR, name)
    with open(path, encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/x-shellscript; charset=utf-8')


@app.get('/agent/install.sh')
def agent_install():
    return _serve_agent_file('install.sh')


@app.get('/agent/uninstall.sh')
def agent_uninstall():
    return _serve_agent_file('uninstall.sh')


# ---------------- Agent API（令牌认证） ----------------

def _agent_registration_transport_ok():
    if ALLOW_INSECURE_AGENT or PANEL_HTTPS:
        return True
    if (_proxy_headers_trusted()
            and request.headers.get('X-Forwarded-Proto', '').lower() == 'https'):
        return True
    if (_is_cloudflare_ip(request.remote_addr or '')
            and request.headers.get('X-Forwarded-Proto', '').lower() == 'https'):
        return True
    try:
        return ipaddress.ip_address(request.remote_addr or '').is_loopback
    except ValueError:
        return False


@app.post('/api/agent/register')
def api_agent_register():
    """Exchange a one-time registration code for the Agent protocol credentials."""
    if not _agent_registration_transport_ok():
        return jsonify({'error': 'Agent 注册要求 HTTPS；请启用 TLS 后重试'}), 403
    ip = _client_ip() or '?'
    locked = db.auth_limited_seconds(f'register:{ip}')
    if locked:
        return jsonify({'error': f'注册尝试过多，请 {locked} 秒后再试'}), 429
    data = request.get_json(force=True, silent=True) or {}
    code = str(data.get('code') or '').strip().upper()
    m = db.consume_registration_code(code)
    if not m:
        db.record_auth_failure(
            f'register:{ip}', window=600, max_fails=10, lock_seconds=600)
        return jsonify({'error': '注册码无效、过期或已使用'}), 403
    db.clear_auth_failures(f'register:{ip}')
    return jsonify({
        'machine_id': m['id'],
        'token': m['token'],
        'sign_key': m['sign_key'],
        'agent_protocol': 2,
    })


def _agent_machine():
    return db.get_machine_by_token(request.headers.get('X-Agent-Token', ''))


@app.post('/api/agent/heartbeat')
def agent_heartbeat():
    m = _agent_machine()
    if not m:
        # 已删除机器的残留 Agent：下发结构化卸载任务。
        tomb = db.pop_tombstone(request.headers.get('X-Agent-Token', ''))
        if tomb:
            payload_b64 = base64.b64encode(tomb['payload'].encode('utf-8')).decode('ascii')
            body = ('status=ok\nprotocol=2\njob_id=tombstone-uninstall\ntimeout=60\n'
                    f"payload_b64={payload_b64}\nsig={tomb['sig']}\n")
            return Response(body, mimetype='text/plain; charset=utf-8')
        return jsonify({'error': 'invalid token'}), 403
    if request.headers.get('X-Agent-Protocol') != '2':
        db.mark_agent_incompatible(
            m['id'],
            'Agent 协议版本过旧；请重新生成接入码并重跑 Agent 安装脚本')
        return Response('status=upgrade_required\nprotocol=2\n',
                        mimetype='text/plain; charset=utf-8')
    m = db.touch_machine(m['id'], request.headers.get('X-Agent-Host', ''),
                         _client_ip())
    job = db.next_queued_job(m['id'])
    lines = ['status=ok', 'protocol=2']
    if job:
        try:
            params = json.loads(job['params'])
            payload = job_types.encode(job['kind'], params)
        except Exception:
            db.fail_job(job['id'], '任务参数无效，已拒绝下发')
            return Response('status=error\nprotocol=2\n',
                            mimetype='text/plain; charset=utf-8')
        payload_b64 = base64.b64encode(payload.encode('utf-8')).decode('ascii')
        lines += [f"job_id={job['id']}", f"kind={job['kind']}",
                  f"timeout={job['timeout']}", f'payload_b64={payload_b64}',
                  f"sig={_job_sig(m['sign_key'], job['id'], payload_b64)}"]
    return Response('\n'.join(lines) + '\n', mimetype='text/plain; charset=utf-8')


@app.post('/api/agent/output')
def agent_output():
    m = _agent_machine()
    if not m:
        return jsonify({'error': 'invalid token'}), 403
    # 长任务期间 Agent 主循环阻塞在命令执行上，输出回传就是活着的证明。
    db.touch_machine_seen(m['id'], request.headers.get('X-Agent-Host', ''))
    jid = request.form.get('job_id', type=int)
    job = db.get_job(jid) if jid else None
    if not job or job['machine_id'] != m['id']:
        return jsonify({'error': 'job not found'}), 404
    if job['status'] != 'running':
        return jsonify({'error': 'job already finished'}), 410
    text = request.form.get('text', '')
    if text:
        db.append_job_output(jid, text)
    if request.form.get('done') == '1':
        exit_code = request.form.get('exit_code', type=int)
        db.finish_job(jid, exit_code if exit_code is not None else -1)
    return Response('status=ok\n', mimetype='text/plain; charset=utf-8')


# ---------------- 测试任务 ----------------

@app.post('/api/runs')
def api_run_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        target_id = int(data.get('target_id'))
        backend_ids = [int(x) for x in (data.get('backend_ids') or [])]
        # 默认 IPv4（两端都用 IPv4，避免机器缺 IPv6 时失败）；ip_version=6 才走 IPv6
        ip_version = 6 if str(data.get('ip_version') or '4').strip() == '6' else 4
        run_id = runner.start_run(target_id, backend_ids, ip_version, params=data)
    except (RuntimeError, ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'run_id': run_id})


@app.get('/api/runs')
def api_runs():
    runs = db.get_runs()
    schedule_names = {s['id']: s['name'] for s in db.get_schedules(include_archived=True)}
    for r in runs:
        total, done, failed = db.run_counts(r['id'])
        r['backend_total'] = total
        r['done_count'] = done
        r['fail_count'] = failed
        if r.get('schedule_id'):
            r['schedule_name'] = schedule_names.get(
                r['schedule_id'], f"定时任务 #{r['schedule_id']}")
    return jsonify(runs)


@app.get('/api/runs/<int:rid>')
def api_run_detail(rid):
    run = db.get_run(rid)
    if not run:
        return jsonify({'error': '测试记录不存在'}), 404
    items = db.get_run_items(rid)
    for it in items:
        it['metrics_obj'] = json.loads(it['metrics']) if it['metrics'] else None
    # 按当前简洁格式重新生成，历史记录也不必重新测试。
    run['report'] = quality.build_report(run, {
        'name': run.get('target_name') or '—',
        'host': run.get('target_host') or '',
        'region': run.get('target_region') or '',
        'bandwidth': run.get('target_bandwidth') or '',
    }, items)
    log_data = runner.get_log_data(rid)
    return jsonify({
        'run': run,
        'items': items,
        'summary': quality.run_summary(run, items),
        'events': log_data['events'],
        'log': log_data['raw_lines'],
    })


@app.post('/api/runs/<int:rid>/stop')
def api_run_stop(rid):
    if not runner.stop_run(rid):
        return jsonify({'error': '该测试未在运行'}), 400
    return jsonify({'ok': True})


@app.delete('/api/runs/<int:rid>')
def api_run_delete(rid):
    if runner.stop_run(rid):
        return jsonify({'error': '测试进行中，请先停止再删除'}), 400
    if not db.get_run(rid):
        return jsonify({'error': '测试记录不存在'}), 404
    db.delete_run(rid)
    return jsonify({'ok': True})


@app.get('/api/runs/<int:rid>/report')
def api_run_report(rid):
    run = db.get_run(rid)
    if not run:
        return jsonify({'error': '测试记录不存在'}), 404
    items = db.get_run_items(rid)
    report = quality.build_report(run, {
        'name': run.get('target_name') or '—',
        'host': run.get('target_host') or '',
        'region': run.get('target_region') or '',
        'bandwidth': run.get('target_bandwidth') or '',
    }, items)
    return Response(
        report,
        mimetype='text/markdown; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename=iperf3-report-{rid}.md'})


@app.get('/api/status')
def api_status():
    return jsonify({'active_run_id': runner.active_run_id(), 'version': APP_VERSION})


# ---------------- 定时任务（长期重复测试 + 跨轮对比） ----------------

_CACHE_LOCK = threading.Lock()
_COMPARISON_CACHE = {}
_REPORT_CACHE = {}


def _schedule_version(s):
    return (s['id'], s.get('last_run_id'), s.get('run_count'), s.get('last_status'),
            s.get('enabled'), s.get('name'), s.get('interval_seconds'),
            s.get('archived_at'))


def _cache_get(cache, key):
    with _CACHE_LOCK:
        return cache.get(key)


def _cache_put(cache, key, value, max_items=32):
    with _CACHE_LOCK:
        if len(cache) >= max_items:
            cache.pop(next(iter(cache)))
        cache[key] = value


@app.get('/api/schedules')
def api_schedules():
    return jsonify([scheduler.schedule_summary(s) for s in db.get_schedules()])


def _schedule_payload(data, base=None):
    """先做参数校验（和手动测试同一套），再收敛定时任务自身的字段。"""
    merged = dict(data)
    if base:
        for key in ('streams', 'duration', 'port', 'ping_count', 'udp',
                    'udp_bandwidth', 'ports'):
            if key not in merged and base.get(key) is not None:
                merged[key] = base.get(key)
    params = runner.normalize_params(merged)
    fields = scheduler.normalize_schedule(data, base=base)
    fields.update({k: params[k] for k in ('streams', 'duration', 'port', 'ping_count',
                                          'udp_bandwidth', 'ports')})
    fields['udp'] = 1 if params['udp'] else 0
    return fields


@app.post('/api/schedules')
def api_schedule_create():
    data = request.get_json(force=True, silent=True) or {}
    fields = _schedule_payload(data)
    fields['ends_at'] = scheduler.compute_end(fields['lifetime_seconds'])
    # 新建任务先标记为“现在到点”，再由 trigger 立即尝试首轮；无论成功、
    # 冲突还是机器离线，trigger 都会把下一次排期推进到正常间隔。
    fields['next_run_at'] = scheduler.now_str()
    s = db.create_schedule(fields)
    try:
        started, start_note = scheduler.trigger(s['id'], manual=True)
    except Exception as e:
        started, start_note = False, f'首轮启动失败：{e}'
        db.update_schedule(s['id'], {
            'last_status': 'failed',
            'last_error': start_note,
            'next_run_at': scheduler.compute_next(s['interval_seconds']),
        })
    s = db.get_schedule(s['id'])
    out = scheduler.schedule_summary(s)
    out['start_attempted'] = True
    out['started'] = bool(started)
    out['start_note'] = start_note
    return jsonify(out)


@app.get('/api/schedules/<int:sid>')
def api_schedule_get(sid):
    s = db.get_schedule(sid)
    if not s:
        return jsonify({'error': '定时任务不存在'}), 404
    out = scheduler.schedule_summary(s)
    out['runs'] = db.get_schedule_runs(sid, limit=30)
    runs = {r['id']: r for r in db.get_runs(limit=100)}
    for sr in out['runs']:
        r = runs.get(sr['run_id']) if sr['run_id'] else None
        if r:
            total, done, failed = db.run_counts(r['id'])
            sr['run_status'] = r['status']
            sr['done_count'] = done
            sr['backend_total'] = total
    return jsonify(out)


@app.put('/api/schedules/<int:sid>')
def api_schedule_update(sid):
    old = db.get_schedule(sid)
    if not old:
        return jsonify({'error': '定时任务不存在'}), 404
    data = request.get_json(force=True, silent=True) or {}
    fields = _schedule_payload(data, base=old)
    # 只改名字/参数时保留原结束时间；改动运行时长才从当前时刻重新计时
    if int(fields['lifetime_seconds']) != int(old.get('lifetime_seconds') or 0):
        new_end = scheduler.compute_end(fields['lifetime_seconds'])
        fields['ends_at'] = new_end
        # 页面上的「延长时间」即恢复已到期任务；普通停用任务保持停用
        if scheduler.is_expired(old) and (not new_end or not scheduler.is_expired({'ends_at': new_end})):
            fields['enabled'] = 1
            fields['next_run_at'] = scheduler.compute_next(fields['interval_seconds'])
    # 间隔变了就按新间隔重排下一次；否则保留原有排队时间（避免改个名字就重置周期）
    if int(fields['interval_seconds']) != int(old.get('interval_seconds') or 0):
        fields['next_run_at'] = scheduler.compute_next(fields['interval_seconds'])
    elif not old.get('next_run_at'):
        fields['next_run_at'] = scheduler.compute_next(fields['interval_seconds'])
    s = db.update_schedule(sid, fields)
    return jsonify(scheduler.schedule_summary(s))


@app.delete('/api/schedules/<int:sid>')
def api_schedule_delete(sid):
    if not db.get_schedule(sid):
        return jsonify({'error': '定时任务不存在'}), 404
    db.delete_schedule(sid)
    return jsonify({'ok': True})


@app.post('/api/schedules/<int:sid>/toggle')
def api_schedule_toggle(sid):
    s = db.get_schedule(sid)
    if not s:
        return jsonify({'error': '定时任务不存在'}), 404
    want = not s['enabled']
    if want and scheduler.is_expired(s):
        return jsonify({'error': '任务已到期，请先编辑并延长运行时长'}), 400
    fields = {'enabled': 1 if want else 0}
    # 重新启用时从「现在」开始计时，避免停用很久后立刻触发一串积压的轮次
    if want:
        fields['next_run_at'] = scheduler.compute_next(s['interval_seconds'])
    s = db.update_schedule(sid, fields)
    return jsonify(scheduler.schedule_summary(s))


@app.post('/api/schedules/<int:sid>/run-now')
def api_schedule_run_now(sid):
    if not db.get_schedule(sid):
        return jsonify({'error': '定时任务不存在'}), 404
    ok, note = scheduler.trigger(sid, manual=True)
    if not ok:
        return jsonify({'error': note}), 400
    return jsonify({'ok': True, 'note': note})


@app.get('/api/schedules/<int:sid>/comparison')
def api_schedule_comparison(sid):
    s = db.get_schedule(sid, include_archived=True)
    limit = request.args.get('limit', scheduler.ROUND_LIMIT, type=int) or scheduler.ROUND_LIMIT
    limit = max(1, min(limit, scheduler.ROUND_LIMIT))
    raw_view_limit = request.args.get('view_limit', type=int)
    view_limit = raw_view_limit if raw_view_limit is not None else scheduler.VIEW_ROUNDS
    view_limit = max(1, min(view_limit, scheduler.ROUND_LIMIT))
    key = (_schedule_version(s), limit, view_limit) if s else ('history', sid, limit, view_limit)
    data = _cache_get(_COMPARISON_CACHE, key)
    if data is None:
        data = scheduler.comparison_data(sid, limit=limit, view_limit=view_limit)
        if not data:
            return jsonify({'error': '该定时任务没有可查看的历史记录'}), 404
        _cache_put(_COMPARISON_CACHE, key, data)
    return jsonify(data)


@app.get('/api/schedules/<int:sid>/report')
def api_schedule_report(sid):
    s = db.get_schedule(sid, include_archived=True)
    key = _schedule_version(s) if s else ('history', sid)
    report = _cache_get(_REPORT_CACHE, key)
    if report is None:
        report = scheduler.build_comparison_report(sid)
        if not report:
            return jsonify({'error': '该定时任务没有可导出的历史记录'}), 404
        _cache_put(_REPORT_CACHE, key, report, max_items=16)
    return Response(
        report,
        mimetype='text/markdown; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename=iperf3-longrun-{sid}.md'})


@app.errorhandler(ValueError)
def handle_value_error(e):
    return jsonify({'error': str(e)}), 400


@app.errorhandler(RuntimeError)
def handle_runtime_error(e):
    """参数/状态类错误统一转成 400 并把原因原样带回前端。

    这些错误都是「用户填错了」或「现在不能这么做」（参数越界、机器离线、
    面板已有测试在跑……），消息本身就是给用户看的操作指引。
    """
    return jsonify({'error': str(e) or '请求无法完成'}), 400


if __name__ == '__main__':
    db.init_db()
    runner.start_discovery_worker()
    runner.start_orphan_reclaimer()
    scheduler.start_scheduler()
    from waitress import serve

    def run_agent_listener():
        serve(app, host='0.0.0.0', port=AGENT_INTERNAL_PORT, threads=4,
              clear_untrusted_proxy_headers=False)

    # 管理端口只由 Compose 绑定到回环地址；Agent 端口单独监听并限制路由。
    threading.Thread(target=run_agent_listener, daemon=True).start()
    serve(app, host='0.0.0.0', port=8000, threads=8,
          clear_untrusted_proxy_headers=False)
