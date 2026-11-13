"""面板推送 Agent 更新：脚本提取 / 摘要 / 端点 / 权限边界。

直接运行: python tests/test_agent_update.py
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_data = tempfile.TemporaryDirectory(prefix='linkpilot-update-')
os.environ['DATA_DIR'] = _data.name

import app.app as app_mod  # noqa: E402
from app import db, job_types  # noqa: E402
from app.app import app as flask_app  # noqa: E402


def machine(name='m1', role='backend'):
    return db.create_machine({'name': name, 'role': role, 'region': '', 'bandwidth': ''})


class AgentScriptTest(unittest.TestCase):
    def test_body_matches_installer_payload(self):
        body, sha = app_mod.agent_script()
        self.assertTrue(body.startswith('#!/bin/bash'))
        self.assertNotIn('\nAGENT_EOF\n', body)
        self.assertEqual(sha, hashlib.sha256(body.encode('utf-8')).hexdigest())
        # 独立再抽一遍，确认缓存不会让内容漂移
        lines = (ROOT / 'app' / 'agent' / 'install.sh').read_text(encoding='utf-8').splitlines()
        start = next(i for i, l in enumerate(lines)
                     if l.strip().startswith('cat > /usr/local/lib/linkpilot/agent.sh <<'))
        end = next(i for i, l in enumerate(lines[start:], start) if l.strip() == 'AGENT_EOF')
        self.assertEqual('\n'.join(lines[start + 1:end]) + '\n', body)

    def test_body_exposes_update_capability(self):
        body, _sha = app_mod.agent_script()
        # 新版 Agent 必须：自报摘要/能力、支持 update_agent、下载后校验 sha256
        self.assertIn('X-Agent-Script', body)
        self.assertIn('X-Agent-Caps', body)
        self.assertIn('update_agent)', body)
        self.assertIn('update.request', body)

    def test_endpoint_is_public_and_byte_identical(self):
        client = flask_app.test_client()
        body, _sha = app_mod.agent_script()
        resp = client.get('/agent/agent.sh')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), body)

    def test_short_bootstrap_embeds_origin_and_code(self):
        client = flask_app.test_client()
        resp = client.get(
            '/agent/install/ABCDEFGHIJ.sh',
            base_url='https://fleet.example')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/x-shellscript', resp.content_type)
        self.assertEqual(resp.headers.get('Cache-Control'), 'no-store')
        body = resp.get_data(as_text=True)
        self.assertTrue(body.startswith('#!/bin/sh'))
        self.assertIn('PANEL_URL=https://fleet.example', body)
        self.assertIn('REG_CODE=ABCDEFGHIJ', body)
        self.assertIn('apk add --no-cache bash', body)
        self.assertIn('bash -s -- "$PANEL_URL" "$REG_CODE"', body)
        checked = subprocess.run(
            ['sh', '-n'], input=body, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_short_bootstrap_rejects_bad_code(self):
        client = flask_app.test_client()
        resp = client.get('/agent/install/bad.sh')
        self.assertEqual(resp.status_code, 400)


class UpdateEndpointTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        self.client = flask_app.test_client()
        # 管理接口都要求登录：测试里直接把会话判定打成已登录
        self._auth = patch.object(app_mod, '_session_user', return_value='tester')
        self._auth.start()
        self.addCleanup(self._auth.stop)

    def test_machine_view_never_leaks_update_key(self):
        m = machine('view')
        db.set_agent_meta(m['id'], 'a' * 64, 'update')
        rows = self.client.get('/api/machines').get_json()
        row = [r for r in rows if r['id'] == m['id']][0]
        self.assertNotIn('update_key', row)
        self.assertNotIn('token', row)
        self.assertNotIn('sign_key', row)
        self.assertEqual(row['agent_hash'], 'a' * 64)
        self.assertTrue(row['agent_push_supported'])
        self.assertTrue(row['agent_outdated'])

    def test_heartbeat_records_hash_and_caps(self):
        m = machine('hb')
        c = flask_app.test_client()
        c.post('/api/agent/heartbeat', headers={
            'X-Agent-Token': m['token'], 'X-Agent-Protocol': '2',
            'X-Agent-Script': 'b' * 64, 'X-Agent-Caps': 'update',
        })
        row = db.get_machine(m['id'])
        self.assertEqual(row['agent_hash'], 'b' * 64)
        self.assertEqual(row['agent_caps'], 'update')

    def test_outdated_machine_gets_signed_job(self):
        m = machine('old')
        db.set_agent_meta(m['id'], 'c' * 64, 'update')
        db.touch_machine(m['id'], 'h', '192.0.2.20')
        resp = self.client.post(f"/api/machines/{m['id']}/update-agent")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        self.assertFalse(data.get('already_current'))
        job = db.get_job(data['job_id'])
        self.assertEqual(job['kind'], 'update_agent')
        params = job_types.normalize('update_agent', __import__('json').loads(job['params']))
        self.assertEqual(params['sha256'], data['sha256'])
        self.assertRegex(params['sig'], r'^[0-9a-f]{64}$')
        self.assertGreater(params['ts'], 0)

    def test_unsupported_agent_is_refused_with_hint(self):
        m = machine('legacy')
        db.set_agent_meta(m['id'], 'd' * 64, '')
        db.touch_machine(m['id'], 'h', '192.0.2.21')
        resp = self.client.post(f"/api/machines/{m['id']}/update-agent")
        self.assertEqual(resp.status_code, 400)
        self.assertIn('接入命令', resp.get_json()['error'])

    def test_up_to_date_machine_is_noop(self):
        _body, sha = app_mod.agent_script()
        m = machine('new')
        db.set_agent_meta(m['id'], sha, 'update')
        db.touch_machine(m['id'], 'h', '192.0.2.22')
        resp = self.client.post(f"/api/machines/{m['id']}/update-agent")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['already_current'])

    def test_update_all_skips_offline_and_legacy(self):
        _body, sha = app_mod.agent_script()
        fresh = machine('fresh')
        db.set_agent_meta(fresh['id'], sha, 'update')
        ok = machine('ok')
        db.set_agent_meta(ok['id'], 'e' * 64, 'update')
        db.touch_machine(ok['id'], 'h', '192.0.2.23')
        legacy = machine('legacy2')
        db.set_agent_meta(legacy['id'], 'f' * 64, '')
        db.touch_machine(legacy['id'], 'h', '192.0.2.24')
        data = self.client.post('/api/machines/update-all').get_json()
        queued_ids = [q['id'] for q in data['queued']]
        self.assertIn(ok['id'], queued_ids)
        self.assertNotIn(fresh['id'], queued_ids)
        skipped = {s['id']: s['reason'] for s in data['skipped']}
        self.assertIn(legacy['id'], skipped)
        self.assertIn('接入命令', skipped[legacy['id']])

    def test_agent_rejecting_update_clears_capability(self):
        """旧 Agent 回报「不认识 update_agent」后，面板不再继续推。"""
        m = machine('reject')
        db.set_agent_meta(m['id'], 'a' * 64, 'update')
        db.touch_machine(m['id'], 'h', '192.0.2.26')
        jid = self.client.post(f"/api/machines/{m['id']}/update-agent").get_json()['job_id']
        db.next_queued_job(m['id'])          # Agent 领取
        resp = flask_app.test_client().post('/api/agent/output', data={
            'job_id': str(jid), 'text': '拒绝未知任务类型：update_agent',
            'done': '1', 'exit_code': '2',
        }, headers={'X-Agent-Token': m['token']})
        self.assertEqual(resp.status_code, 200)
        row = db.get_machine(m['id'])
        self.assertEqual(row['agent_caps'], '')
        self.assertEqual(db.get_job(jid)['status'], 'failed')
        # 再点更新会被明确拒绝，而不是白排一次任务
        again = self.client.post(f"/api/machines/{m['id']}/update-agent")
        self.assertEqual(again.status_code, 400)

    def test_registration_returns_update_key(self):
        m = machine('reg')
        db.touch_machine(m['id'], 'h', '192.0.2.25')
        code = db.create_registration_code(m['id'])
        c = flask_app.test_client()
        resp = c.post('/api/agent/register', json={'code': code})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        self.assertRegex(data['update_key'], r'^[0-9a-f]{64}$')
        self.assertNotEqual(data['update_key'], data['sign_key'])

    def test_update_key_is_rotated_on_registration(self):
        m = machine('rot')
        before = db.get_machine(m['id'])['update_key']
        code = db.create_registration_code(m['id'])
        flask_app.test_client().post('/api/agent/register', json={'code': code})
        after = db.get_machine(m['id'])['update_key']
        self.assertNotEqual(before, after)
        self.assertRegex(after, r'^[0-9a-f]{64}$')


class ApplyHelperTest(unittest.TestCase):
    """安装脚本里必须带 root 验签助手 + 路径单元，否则推送更新没有落地执行者。"""

    def setUp(self):
        self.text = (ROOT / 'app' / 'agent' / 'install.sh').read_text(encoding='utf-8')

    def test_helper_verifies_hmac_before_install(self):
        self.assertIn('apply-update.sh', self.text)
        self.assertIn('openssl dgst -sha256 -hmac', self.text)
        self.assertIn('/etc/linkpilot/update.key', self.text)
        self.assertIn('bash -n', self.text)
        self.assertIn('systemctl restart linkpilot-agent', self.text)
        # 只有 root 可读的更新密钥
        self.assertIn('chmod 0600 /etc/linkpilot/update.key', self.text)
        self.assertIn('chown root:root /etc/linkpilot/update.key', self.text)

    def test_path_unit_watches_request_file(self):
        self.assertIn('linkpilot-agent-update.path', self.text)
        self.assertIn('PathExists=/var/lib/linkpilot/update.request', self.text)


class PodmanCompatTest(unittest.TestCase):
    """受限 Podman 必须由安装器自动降级，不能让用户手工改 systemd。"""

    def setUp(self):
        self.text = (ROOT / 'app' / 'agent' / 'install.sh').read_text(encoding='utf-8')

    def test_detects_limited_container_capabilities(self):
        self.assertIn('container_kind()', self.text)
        self.assertIn('cap_available()', self.text)
        self.assertIn('CAP_SYS_ADMIN', self.text)
        self.assertIn('CAP_NET_RAW', self.text)
        self.assertIn('LINKPILOT_CONTAINER_COMPAT', self.text)

    def test_renders_container_compatible_unit(self):
        self.assertIn('if [ "$SANDBOX_COMPAT" = "1" ]', self.text)
        self.assertIn('if [ "$PING_COMPAT" = "1" ]', self.text)
        self.assertIn('SupplementaryGroups=root', self.text)
        self.assertIn('/usr/local/lib/linkpilot/ping', self.text)
        self.assertIn('setcap -r "$PING_DST"', self.text)
        self.assertIn('Environment=PATH=/usr/local/lib/linkpilot:', self.text)
        self.assertIn('PATH="/usr/local/lib/linkpilot:$PATH"', self.text)
        self.assertIn('ping_group_range', self.text)

    def test_installer_removes_legacy_compat_dropin(self):
        self.assertIn('LEGACY_COMPAT_DROPIN', self.text)
        self.assertIn('The host is a restricted Podman container', self.text)
        self.assertIn('rm -f /etc/systemd/system/linkpilot-agent.service.d/container-compat.conf',
                      self.text)


class AlpineCompatTest(unittest.TestCase):
    """Alpine/OpenRC 需要 bash 引导、受监督守护和源码更新 watcher。"""

    def setUp(self):
        self.text = (ROOT / 'app' / 'agent' / 'install.sh').read_text(encoding='utf-8')
        self.app_js = (ROOT / 'app' / 'static' / 'app.js').read_text(encoding='utf-8')

    def test_browser_command_is_short_and_downloads_bootstrap(self):
        self.assertIn('/agent/install/${encodeURIComponent(', self.app_js)
        self.assertIn('|| wget -qO-', self.app_js)
        self.assertIn(') | sh`', self.app_js)

    def test_installer_emits_openrc_services(self):
        self.assertIn('HAS_OPENRC', self.text)
        self.assertIn('/etc/init.d/linkpilot-agent', self.text)
        self.assertIn('/etc/init.d/linkpilot-agent-watch', self.text)
        self.assertIn('supervise-daemon "$RC_SVCNAME" --start', self.text)
        self.assertIn('--user linkpilot --group __AGENT_GROUP__ --no-new-privs', self.text)
        self.assertIn('rc-update add linkpilot-agent default', self.text)

    def test_alpine_uses_unprivileged_private_ping(self):
        self.assertIn('ensure_pkg ping iputils-ping iputils iputils-ping', self.text)
        self.assertIn('PING_COMPAT=1', self.text)
        self.assertIn('/etc/linkpilot/agent.group', self.text)
        self.assertIn('watch-root-requests.sh', self.text)
        self.assertIn('rc-service linkpilot-agent restart', self.text)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        _data.cleanup()
