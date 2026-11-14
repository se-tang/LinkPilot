"""LinkPilot 面板 Web 服务：登录验证、机器管理（Agent 接入）、测试任务、Agent API。"""
import base64
import csv
import datetime
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import shlex
import threading
import time
from urllib.parse import urlsplit

from flask import (Flask, Response, g, jsonify, redirect, render_template, request,
                   session, stream_with_context)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, job_types, quality, runner, scheduler

# 版本号：这是唯一权威（面板页面、部署横幅、install.sh 都从/沿用它取）。
# 还在测试阶段，因此用 0.x 版本号。
APP_VERSION = '0.10.8'

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
TRUSTED_PROXY_NETWORKS = []
for _network in os.environ.get('PANEL_TRUSTED_PROXY_CIDRS', '').split(','):
    try:
        if _network.strip():
            TRUSTED_PROXY_NETWORKS.append(ipaddress.ip_network(_network.strip(), strict=False))
    except ValueError:
        continue

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
    try:
        if int(request.environ.get('SERVER_PORT', '0')) == AGENT_INTERNAL_PORT:
            return False
        remote = ipaddress.ip_address(request.remote_addr or '')
    except ValueError:
        return False
    if any(remote in network for network in TRUSTED_PROXY_NETWORKS):
        return True
    if not TRUST_LOCAL_PROXY:
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


def _request_origin():
    """Return the browser-facing origin, including a terminating HTTPS proxy."""
    scheme = request.headers.get('X-Forwarded-Proto', '').split(',')[0].strip().lower()
    if scheme not in ('http', 'https'):
        scheme = request.scheme
    host = request.headers.get('X-Forwarded-Host', '').split(',')[0].strip() or request.host
    prefix = request.headers.get('X-Forwarded-Prefix', '').split(',')[0].strip()
    if prefix and not prefix.startswith('/'):
        prefix = '/' + prefix
    candidate = f'{scheme}://{host}{prefix}'.rstrip('/')
    parsed = urlsplit(candidate)
    if parsed.scheme in ('http', 'https') and parsed.netloc:
        return candidate
    return request.url_root.rstrip('/')


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
        # SSE 必须 no-cache（no-store 也可以，但明确写成 no-cache 更贴合事件流语义）
        resp.headers['Cache-Control'] = 'no-cache' if request.path == '/api/stream' else 'no-store'
    elif request.path.startswith('/static/'):
        # 模板里给静态资源带了 ?v=<版本号>：带版本号可以长缓存，没带的只缓存一小时
        if request.args.get('v'):
            resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        else:
            resp.headers['Cache-Control'] = 'public, max-age=3600'
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
    return render_template(
        'index.html', version=APP_VERSION, agent_url=_request_origin(),
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

def _machine_view(m):
    """机器列表/详情的统一视图：补上「Agent 是否需要更新、能不能推送」。"""
    row = db.public_machine(m)
    _body, sha = agent_script()
    caps = str(row.get('agent_caps') or '')
    row['agent_push_supported'] = 'update' in caps
    row['agent_hash'] = row.get('agent_hash') or ''
    row['agent_outdated'] = bool(row['agent_hash']) and row['agent_hash'] != sha
    row['agent_script_sha'] = sha[:12]
    return row


@app.get('/api/machines')
def api_machines():
    return jsonify([_machine_view(m) for m in db.get_machines()])


@app.get('/api/machines/<int:mid>')
def api_machine_get(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    return jsonify(_machine_view(m))


def _agent_update_payload(m):
    """构造推送更新任务：脚本摘要 + 时间戳 + 用该机器的 root 更新密钥签的 HMAC。"""
    _body, sha = agent_script()
    key = str(m.get('update_key') or '')
    if not key:
        raise RuntimeError('该机器没有更新密钥，请重新生成接入码并重跑接入命令')
    ts = int(time.time())
    sig = hmac.new(key.encode('utf-8'), f'update:{sha}:{ts}'.encode('utf-8'),
                   hashlib.sha256).hexdigest()
    return {'sha256': sha, 'ts': ts, 'sig': sig}, sha


@app.post('/api/machines/<int:mid>/update-agent')
def api_machine_update_agent(mid):
    """让这台机器的 Agent 自更新到面板当前版本（由 Agent 下载 + root 助手替换）。"""
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    if not db.machine_online(m):
        return jsonify({'error': '该机器 Agent 未上线，无法推送更新'}), 400
    if 'update' not in str(m.get('agent_caps') or ''):
        return jsonify({
            'error': '这台机器的 Agent 还不支持推送更新（缺少 root 更新助手），'
                     '需要重新生成接入码并重跑一次接入命令'}), 400
    _body, sha = agent_script()
    if (m.get('agent_hash') or '') == sha:
        return jsonify({'ok': True, 'already_current': True, 'sha256': sha})
    params, sha = _agent_update_payload(m)
    job_id = db.create_job(mid, 'update_agent', params, timeout=300)
    return jsonify({'ok': True, 'job_id': job_id, 'sha256': sha})


@app.post('/api/machines/update-all')
def api_machines_update_all():
    """给所有「旧版本 + 在线 + 支持推送」的机器排队一次更新。"""
    _body, sha = agent_script()
    queued, skipped = [], []
    for m in db.get_machines():
        if (m.get('agent_hash') or '') == sha:
            continue
        if not db.machine_online(m):
            skipped.append({'id': m['id'], 'name': m['name'], 'reason': 'Agent 离线'})
            continue
        if 'update' not in str(m.get('agent_caps') or ''):
            skipped.append({'id': m['id'], 'name': m['name'],
                            'reason': '不支持推送更新，需重跑接入命令'})
            continue
        params, _sha = _agent_update_payload(m)
        queued.append({'id': m['id'], 'name': m['name'],
                       'job_id': db.create_job(m['id'], 'update_agent', params, 300)})
    return jsonify({'ok': True, 'sha256': sha, 'queued': queued, 'skipped': skipped})


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
    return jsonify(_machine_view(db.create_machine(fields)))


@app.put('/api/machines/<int:mid>')
def api_machine_update(mid):
    m = db.get_machine(mid)
    if not m:
        return jsonify({'error': '机器不存在'}), 404
    data = request.get_json(force=True, silent=True) or {}
    fields = _machine_fields(data)
    return jsonify(_machine_view(db.update_machine(mid, fields)))


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
    return jsonify({
        'machine_id': mid,
        'code': code,
        'agent_url': _request_origin(),
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


@app.get('/agent/install/<code>.sh')
def agent_install_bootstrap(code):
    """Serve a short POSIX bootstrap carrying the one-time code and panel URL."""
    code = str(code or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9]{10}', code):
        return Response('invalid registration code\n', status=400, mimetype='text/plain')
    panel_url = _request_origin()
    script = f'''#!/bin/sh
set -eu
PANEL_URL={shlex.quote(panel_url)}
REG_CODE={shlex.quote(code)}

if ! command -v bash >/dev/null 2>&1; then
  if command -v apk >/dev/null 2>&1; then
    apk add --no-cache bash
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y bash
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y bash
  elif command -v yum >/dev/null 2>&1; then
    yum install -y bash
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install bash
  else
    printf "LinkPilot requires bash\\n" >&2
    exit 1
  fi
fi

download_installer() {{
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$PANEL_URL/agent/install.sh"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$PANEL_URL/agent/install.sh"
  else
    printf "LinkPilot requires curl or wget\\n" >&2
    return 1
  fi
}}

case "$PANEL_URL" in
  http://*) download_installer | LINKPILOT_INSECURE_HTTP=1 bash -s -- "$PANEL_URL" "$REG_CODE" ;;
  *) download_installer | bash -s -- "$PANEL_URL" "$REG_CODE" ;;
esac
'''
    resp = Response(script, mimetype='text/x-shellscript; charset=utf-8')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.get('/agent/uninstall.sh')
def agent_uninstall():
    return _serve_agent_file('uninstall.sh')


# ---------------- Agent 脚本体（供「面板推送更新」下载） ----------------

_AGENT_BODY_CACHE = {'mtime': None, 'body': '', 'sha': ''}


def agent_script():
    """返回 (install.sh 里的 Agent 脚本体, sha256)。

    必须和 install.sh 真正写进 /usr/local/lib/linkpilot/agent.sh 的内容逐字节一致，
    否则 Agent 端按签过名的 sha256 校验必然失败。
    """
    path = os.path.join(AGENT_DIR, 'install.sh')
    mtime = os.path.getmtime(path)
    if _AGENT_BODY_CACHE['mtime'] != mtime:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
        start = end = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if start is None and stripped.startswith(
                    'cat > /usr/local/lib/linkpilot/agent.sh <<'):
                start = i
            elif start is not None and stripped == 'AGENT_EOF':
                end = i
                break
        if start is None or end is None:
            raise RuntimeError('install.sh 里找不到 Agent 脚本体')
        body = '\n'.join(lines[start + 1:end]) + '\n'
        _AGENT_BODY_CACHE.update({
            'mtime': mtime,
            'body': body,
            'sha': hashlib.sha256(body.encode('utf-8')).hexdigest(),
        })
    return _AGENT_BODY_CACHE['body'], _AGENT_BODY_CACHE['sha']


@app.get('/agent/agent.sh')
def agent_body():
    body, _sha = agent_script()
    return Response(body, mimetype='text/x-shellscript; charset=utf-8')


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
        # 根密钥：安装脚本会把它写到 /etc/linkpilot/update.key（0600 root），
        # Agent 账号读不到，用于校验「面板推送的 Agent 更新」
        'update_key': m.get('update_key', ''),
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
    # Agent 自报的脚本摘要 / 能力：面板据此判断「要不要更新、能不能推」
    db.set_agent_meta(
        m['id'],
        request.headers.get('X-Agent-Script', ''),
        request.headers.get('X-Agent-Caps', ''))
    # 任务执行期间 Agent 仍会每几秒发一次心跳，但带上 X-Agent-Busy: 1：
    # 这时只回传「要不要中止」的指令，绝不再派新任务（否则会并发跑两个测速）。
    if request.headers.get('X-Agent-Busy') == '1':
        lines = ['status=ok', 'protocol=2']
        running = db.running_job_id(m['id'])
        if running and db.job_cancel_requested(running):
            lines.append(f'cancel_job={running}')
        return Response('\n'.join(lines) + '\n', mimetype='text/plain; charset=utf-8')
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
        code = exit_code if exit_code is not None else -1
        db.finish_job(jid, code)
        # 旧 Agent 不认识 update_agent（exit 2）时会明确回报；这时收回它的推送能力，
        # 面板改提示「需要重跑接入命令」，而不是每次点更新都白排一次任务。
        if job.get('kind') == 'update_agent' and code == 2:
            db.mark_agent_update_unsupported(m['id'])
            db.set_machine_probe(
                m['id'], 'Agent 版本过旧，不支持推送更新：请重新生成接入码并重跑一次接入命令')
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
    # 一次聚合取回所有轮次的计数，避免每轮各查一次（列表每几秒被前端轮询一次）
    counts = db.run_counts_bulk([r['id'] for r in runs])
    for r in runs:
        total, done, failed = counts.get(r['id'], (0, 0, 0))
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
    """停止测试：面板标志位 + 通知远端 Agent 中止。

    只置面板标志位的话，后端机上的 iperf3/ping 还会跑满时长：既占着目标机端口，
    又会在下一轮开测时抢带宽，整轮结果都不作数。这里同时给涉及的任务打上取消
    标记，Agent 执行期间的忙碌心跳会读到并 kill 掉自己启动的子进程。
    """
    stopped = runner.stop_run(rid)
    cancelled = db.request_job_cancel(db.running_job_ids_of_run(rid))
    if not stopped and not cancelled:
        return jsonify({'error': '该测试未在运行'}), 400
    return jsonify({'ok': True, 'cancelled_jobs': cancelled})


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


# ---------------- 实时状态推送（SSE，替代前端 8/10/15 秒轮询） ----------------
#
# 前端只关心「哪些数据变了」，所以这里推的是四个数据域的指纹，不是数据本身：
# 指纹变了前端才去调对应的 REST 接口，既省流量，也不用把渲染逻辑搬到服务端。

_STREAM_LOCK = threading.Lock()
_STREAM_CLIENTS = 0
_STREAM_MAX_CLIENTS = 4       # waitress 只有 8 个线程，SSE 长连接不能无限占
_STREAM_TTL_SECONDS = 300     # 到点主动断开，让 EventSource 重连（顺带回收线程）
_DIGEST_LOCK = threading.Lock()
_DIGEST_CACHE = {'at': 0.0, 'value': None}


def _sig(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')
    ).hexdigest()[:12]


def compute_state_digest():
    """四个数据域的指纹；测试里可直接调用。"""
    _body, agent_sha = agent_script()
    machines = [
        [m['id'], bool(m.get('online')), bool(m.get('active_job')), m.get('role'),
         m.get('name'), (m.get('agent_hash') or '')[:8], m.get('agent_caps') or '']
        for m in db.get_machines()
    ]
    runs = db.get_runs(limit=30)
    counts = db.run_counts_bulk([r['id'] for r in runs])
    runs_sig = [
        [r['id'], r['status'], r.get('schedule_id'),
         counts.get(r['id'], (0, 0, 0))[1], counts.get(r['id'], (0, 0, 0))[2]]
        for r in runs
    ]
    schedules = [
        [s['id'], bool(s.get('enabled')), s.get('last_status'), s.get('last_run_id'),
         s.get('run_count'), s.get('next_run_at'), s.get('archived_at')]
        for s in db.get_schedules(include_archived=True)
    ]
    return {
        'version': APP_VERSION,
        'agent_script': agent_sha[:12],
        'machines': _sig(machines),
        'runs': _sig(runs_sig),
        'schedules': _sig(schedules),
    }


def state_digest():
    """2 秒内的指纹复用：多个浏览器窗口不必各自算一遍。"""
    now = time.time()
    with _DIGEST_LOCK:
        if _DIGEST_CACHE['value'] is not None and now - _DIGEST_CACHE['at'] < 2:
            return _DIGEST_CACHE['value']
    value = compute_state_digest()
    with _DIGEST_LOCK:
        _DIGEST_CACHE['at'] = now
        _DIGEST_CACHE['value'] = value
    return value


def _stream_acquire():
    global _STREAM_CLIENTS
    with _STREAM_LOCK:
        if _STREAM_CLIENTS >= _STREAM_MAX_CLIENTS:
            return False
        _STREAM_CLIENTS += 1
        return True


def _stream_release():
    global _STREAM_CLIENTS
    with _STREAM_LOCK:
        _STREAM_CLIENTS = max(0, _STREAM_CLIENTS - 1)


@app.get('/api/stream')
def api_stream():
    """SSE：数据有变化才推 state，没变化每 15 秒发一个注释保活。"""
    if not _stream_acquire():
        # 连接数到顶就明确拒绝，前端会自动退回轮询
        return Response('too many streams\n', status=503, mimetype='text/plain')

    def generate():
        try:
            yield 'retry: 3000\n\n'
            deadline = time.time() + _STREAM_TTL_SECONDS
            last = None
            ticks = 0
            while time.time() < deadline:
                if not _session_user():
                    break                      # 会话过期（登出 / 改密码）就结束
                try:
                    payload = state_digest()
                except Exception as exc:        # 单次查询失败不该把连接打死
                    yield f': error {exc.__class__.__name__}\n\n'
                    time.sleep(2)
                    continue
                if payload != last:
                    last = payload
                    yield 'event: state\ndata: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'
                else:
                    ticks += 1
                    if ticks % 8 == 0:
                        yield ': keepalive\n\n'
                time.sleep(2)
            yield 'event: bye\ndata: {"reason":"ttl"}\n\n'
        finally:
            _stream_release()

    resp = Response(stream_with_context(generate()),
                    mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'   # 反代别缓冲，否则 SSE 会攒住不发
    return resp


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


# ---------------- 导出（CSV / JSON） ----------------

_CSV_TRIGGER = ('=', '+', '-', '@')


def _csv_cell(value):
    """防 CSV 公式注入：Excel 会把 = + - @ 开头的单元格当公式执行。"""
    if value is None:
        return ''
    text = str(value)
    if text[:1] in _CSV_TRIGGER:
        text = "'" + text
    return text


def _csv_response(rows, filename):
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow([_csv_cell(cell) for cell in row])
    # 带 BOM：Excel 直接双击打开中文表头才不会乱码（pandas 也能正常处理）
    body = '\ufeff' + buf.getvalue()
    return Response(
        body, mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'})


@app.get('/api/runs/<int:rid>/export.csv')
def api_run_export_csv(rid):
    run = db.get_run(rid)
    if not run:
        return jsonify({'error': '测试记录不存在'}), 404
    items = db.get_run_items(rid)
    rows = [[
        '机器', '地区', '带宽', '端口', '状态', '上行Mbit/s', '下行Mbit/s', '丢包%',
        'RTTms', '抖动ms', '重传', '上行CV%', '下行CV%', '上行P95', '下行P95',
        '评级', '说明',
    ]]
    for it in items:
        mt = it.get('metrics_obj') or {}
        rows.append([
            it.get('machine_name'), it.get('machine_region'), it.get('machine_bandwidth'),
            it.get('port'), it.get('status'), mt.get('up_mbits'), mt.get('down_mbits'),
            mt.get('loss_pct'), mt.get('rtt_avg'), mt.get('mdev_ms'), mt.get('retr_total'),
            mt.get('up_cv_pct'), mt.get('down_cv_pct'), mt.get('up_p95_mbits'),
            mt.get('down_p95_mbits'),
            (mt.get('rating_code') or '') + ' ' + (mt.get('rating_label') or ''),
            mt.get('rating_summary') or it.get('error') or '',
        ])
    return _csv_response(rows, f'linkpilot-run-{rid}.csv')


@app.get('/api/runs/<int:rid>/export.json')
def api_run_export_json(rid):
    run = db.get_run(rid)
    if not run:
        return jsonify({'error': '测试记录不存在'}), 404
    items = db.get_run_items(rid)
    payload = {
        'version': APP_VERSION,
        'run': run,
        'items': [{
            'machine_id': it.get('machine_id'), 'machine_name': it.get('machine_name'),
            'machine_region': it.get('machine_region'),
            'machine_bandwidth': it.get('machine_bandwidth'),
            'port': it.get('port'), 'status': it.get('status'), 'error': it.get('error'),
            'metrics': it.get('metrics_obj'),
        } for it in items],
        'summary': quality.run_summary(run, items),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    return Response(body, mimetype='application/json; charset=utf-8',
                    headers={'Content-Disposition':
                             f'attachment; filename=linkpilot-run-{rid}.json'})


@app.get('/api/schedules/<int:sid>/export.csv')
def api_schedule_export_csv(sid):
    data = scheduler.comparison_data(sid, limit=scheduler.ROUND_LIMIT)
    if not data:
        return jsonify({'error': '该定时任务没有可导出的历史记录'}), 404
    by_round = {}
    for machine in data['machines']:
        for point in machine['series']:
            by_round.setdefault(point['run_id'], {})[machine['name']] = point
    rows = [[
        '轮次', '开始时间', '结束时间', '机器', '状态', '上行Mbit/s', '下行Mbit/s',
        '丢包%', 'RTTms', '抖动ms', '重传', '评级', '说明',
    ]]
    for rnd in data['rounds']:
        points = by_round.get(rnd['run_id'], {})
        for name in [m['name'] for m in data['machines']]:
            point = points.get(name)
            if not point:
                continue
            rows.append([
                rnd['run_id'], rnd['created_at'], rnd.get('finished_at') or '', name,
                point.get('status'), point.get('up'), point.get('down'), point.get('loss'),
                point.get('rtt'), point.get('mdev'), point.get('retr'),
                point.get('rating'), point.get('error') or '',
            ])
    return _csv_response(rows, f'linkpilot-schedule-{sid}.csv')


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
