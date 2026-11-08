"""设置 / 告警 / 导出 / SSE：端到端（本地 HTTP 收报文，不打外网）。

直接运行: python tests/test_settings_export.py
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_data = tempfile.TemporaryDirectory(prefix='linkpilot-settings-')
os.environ['DATA_DIR'] = _data.name

import app.app as app_mod  # noqa: E402
from app import alerts, db  # noqa: E402
from app.app import app as flask_app  # noqa: E402

METRICS_OK = json.dumps({'up_mbits': 500.0, 'down_mbits': 450.0, 'loss_pct': 0.0,
                         'rtt_avg': 10.0, 'mdev_ms': 0.5, 'retr_total': 0,
                         'throughput_baseline_mbps': 500.0,
                         'rating_code': 'C', 'rating_label': '较差'})
METRICS_BAD = json.dumps({'up_mbits': 20.0, 'down_mbits': 18.0, 'loss_pct': 3.5,
                          'rtt_avg': 120.0, 'mdev_ms': 12.0, 'retr_total': 40,
                          'throughput_baseline_mbps': 500.0,
                          'rating_code': 'C', 'rating_label': '较差'})


class WebhookReceiver:
    """本地 HTTP 接收端：记录收到的 JSON 和返回码。"""

    def __init__(self, status=200):
        self.received = []
        self.status = status
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):          # noqa: N802
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length)
                try:
                    outer.received.append(json.loads(body.decode('utf-8')))
                except ValueError:
                    outer.received.append({'_raw': body})
                self.send_response(outer.status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/hook'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def reset_settings():
    for key, value in alerts.DEFAULTS.items():
        db.set_setting(key, value)


def make_run(loss=None, machines=2):
    target = db.create_machine({'name': 't-target', 'role': 'target', 'region': '', 'bandwidth': '1G'})
    backends = [db.create_machine({'name': f'b-{i}', 'role': 'backend', 'region': '',
                                   'bandwidth': '500M'}) for i in range(machines)]
    rid = db.create_run(target, [m['id'] for m in backends], 4, target_host='192.0.2.30')
    for idx, item in enumerate(db.get_run_items(rid)):
        metrics = METRICS_BAD if (loss and idx == 0) else METRICS_OK
        db.update_item(item['id'], status='done', metrics=metrics)
    db.finish_run(rid, 'finished', 'report', 'log')
    return rid


class SettingsTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        reset_settings()
        self.client = flask_app.test_client()
        self.auth = patch.object(app_mod, '_session_user', return_value='tester')
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_defaults_and_save(self):
        cfg = self.client.get('/api/settings').get_json()
        self.assertEqual(cfg['min_loss_pct'], 1.0)
        self.assertEqual(cfg['min_throughput_pct'], 40.0)
        self.assertFalse(cfg['enabled'] and cfg['webhook'])   # 默认没配地址

        saved = self.client.post('/api/settings', json={
            'enabled': True, 'webhook': 'https://example.com/hook',
            'min_loss_pct': 2.5, 'min_throughput_pct': 60, 'cooldown_seconds': 120,
        }).get_json()
        self.assertTrue(saved['enabled'])
        self.assertEqual(saved['webhook'], 'https://example.com/hook')
        self.assertEqual(saved['min_loss_pct'], 2.5)
        self.assertEqual(saved['cooldown_seconds'], 120)
        # 落库后 GET 应一致
        again = self.client.get('/api/settings').get_json()
        self.assertEqual(again, saved)

    def test_validation(self):
        for body, hint in (
            ({'webhook': 'ftp://x'}, 'webhook'),
            ({'webhook': 'https://x', 'min_loss_pct': 200}, '丢包阈值'),
            ({'webhook': 'https://x', 'min_throughput_pct': 0}, '吞吐下限'),
            ({'webhook': 'https://x', 'cooldown_seconds': 999999}, '冷却时间'),
        ):
            resp = self.client.post('/api/settings', json=body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertIn(hint, resp.get_json()['error'])

    def test_test_alert_hits_webhook(self):
        receiver = WebhookReceiver()
        self.addCleanup(receiver.close)
        self.client.post('/api/settings', json={'enabled': True, 'webhook': receiver.url})
        resp = self.client.post('/api/settings/test-alert')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()['status'], 200)
        self.assertEqual(receiver.received[0]['event'], 'test')

    def test_test_alert_requires_webhook(self):
        db.set_setting('alert_webhook', '')
        resp = self.client.post('/api/settings/test-alert')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('webhook', resp.get_json()['error'])


class AlertLogicTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        for key in ('alert_webhook', 'alert_min_loss_pct', 'alert_min_throughput_pct',
                    'alert_cooldown_seconds', 'alert_enabled'):
            db.set_setting(key, alerts.DEFAULTS[key] if key != 'alert_webhook' else '')

    def test_evaluate_flags_loss_and_throughput(self):
        rid = make_run(loss=True)
        run = db.get_run(rid)
        items = db.get_run_items(rid)
        found = alerts.evaluate(run, items, alerts.config())
        self.assertEqual(len(found), 1, found)
        reasons = ' '.join(found[0]['reasons'])
        self.assertIn('丢包 3.50%', reasons)
        self.assertIn('上行吞吐 20.0', reasons)
        self.assertIn('下行吞吐 18.0', reasons)

    def test_evaluate_quiet_when_healthy(self):
        rid = make_run(loss=False)
        found = alerts.evaluate(db.get_run(rid), db.get_run_items(rid), alerts.config())
        self.assertEqual(found, [])

    def test_maybe_send_respects_cooldown(self):
        receiver = WebhookReceiver()
        self.addCleanup(receiver.close)
        db.set_setting('alert_webhook', receiver.url)
        db.set_setting('alert_threshold_placeholder', '')
        rid = make_run(loss=True)
        run = db.get_run(rid)
        items = db.get_run_items(rid)
        self.assertEqual(alerts.maybe_send(run, items), 1)
        # 同一轮、同一台机器：冷却时间内不再发
        self.assertEqual(alerts.maybe_send(run, items), 0)

    def test_maybe_send_disabled_without_webhook(self):
        rid = make_run(loss=True)
        self.assertEqual(alerts.maybe_send(db.get_run(rid), db.get_run_items(rid)), 0)


class ExportTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        reset_settings()
        self.client = flask_app.test_client()
        self.auth = patch.object(app_mod, '_session_user', return_value='tester')
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_run_csv_and_json(self):
        rid = make_run(loss=True)
        csv_resp = self.client.get(f'/api/runs/{rid}/export.csv')
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn('text/csv', csv_resp.headers['Content-Type'])
        self.assertIn(f'linkpilot-run-{rid}.csv', csv_resp.headers['Content-Disposition'])
        text = csv_resp.get_data(as_text=True)
        rows = [line for line in text.splitlines() if line.strip()]
        self.assertIn('上行Mbit/s', rows[0])
        self.assertEqual(len(rows), 1 + 2)      # 表头 + 两台机器
        self.assertIn('b-0', text)

        json_resp = self.client.get(f'/api/runs/{rid}/export.json')
        self.assertEqual(json_resp.status_code, 200)
        payload = json_resp.get_json()
        self.assertEqual(payload['run']['id'], rid)
        self.assertEqual(len(payload['items']), 2)
        self.assertIn('summary', payload)

    def test_csv_escapes_formula_cells(self):
        rid = make_run(loss=False)
        item = db.get_run_items(rid)[0]
        db.update_item(item['id'], status='done', metrics=METRICS_OK)
        db.get_db().execute('UPDATE run_items SET machine_name=? WHERE id=?',
                            ('=1+1', item['id']))
        db.get_db().commit()
        text = self.client.get(f'/api/runs/{rid}/export.csv').get_data(as_text=True)
        self.assertIn("'=1+1", text)

    def test_schedule_csv(self):
        rid = make_run(loss=True)
        sid = db.create_schedule({'name': 'long', 'target_id': 1, 'backend_ids': [2, 3],
                                  'interval_seconds': 300, 'lifetime_seconds': 0})['id']
        db.get_db().execute('UPDATE runs SET schedule_id=? WHERE id=?', (sid, rid))
        db.get_db().commit()
        db.record_schedule_run(sid, rid, '2026-11-09 00:00:00', status='finished')
        resp = self.client.get(f'/api/schedules/{sid}/export.csv')
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True)
        self.assertIn('轮次', text.splitlines()[0])
        self.assertIn('b-0', text)


class StreamTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        reset_settings()
        self.client = flask_app.test_client()
        self.auth = patch.object(app_mod, '_session_user', return_value='tester')
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_digest_changes_with_data(self):
        first = app_mod.compute_state_digest()
        self.assertIn('machines', first)
        make_run(loss=False)
        second = app_mod.compute_state_digest()
        self.assertNotEqual(first['runs'], second['runs'])

    def test_stream_emits_state(self):
        resp = self.client.get('/api/stream', buffered=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/event-stream', resp.headers['Content-Type'])
        self.assertEqual(resp.headers.get('Cache-Control'), 'no-cache')
        chunks = []
        for chunk in resp.response:
            chunks.append(chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk)
            if len(chunks) >= 2:
                break
        resp.close()
        joined = ''.join(chunks)
        self.assertIn('event: state', joined)
        self.assertIn('"machines"', joined)

    def test_unauthenticated_stream_is_401(self):
        with patch.object(app_mod, '_session_user', return_value=''):
            resp = self.client.get('/api/stream')
        self.assertEqual(resp.status_code, 401)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        _data.cleanup()
