"""导出（CSV / JSON）与 SSE 实时推送。

直接运行: python tests/test_export_stream.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_data = tempfile.TemporaryDirectory(prefix='linkpilot-export-')
os.environ['DATA_DIR'] = _data.name

import app.app as app_mod  # noqa: E402
from app import db  # noqa: E402
from app.app import app as flask_app  # noqa: E402

METRICS_OK = json.dumps({'up_mbits': 500.0, 'down_mbits': 450.0, 'loss_pct': 0.0,
                         'rtt_avg': 10.0, 'mdev_ms': 0.5, 'retr_total': 0,
                         'throughput_baseline_mbps': 500.0,
                         'rating_code': 'S', 'rating_label': '优秀'})
METRICS_BAD = json.dumps({'up_mbits': 20.0, 'down_mbits': 18.0, 'loss_pct': 3.5,
                          'rtt_avg': 120.0, 'mdev_ms': 12.0, 'retr_total': 40,
                          'throughput_baseline_mbps': 500.0,
                          'rating_code': 'C', 'rating_label': '较差'})


def make_run(bad=False, machines=2):
    target = db.create_machine({'name': 't-target', 'role': 'target',
                                'region': '', 'bandwidth': '1G'})
    backends = [db.create_machine({'name': f'b-{i}', 'role': 'backend', 'region': '',
                                   'bandwidth': '500M'}) for i in range(machines)]
    rid = db.create_run(target, [m['id'] for m in backends], 4, target_host='192.0.2.30')
    for idx, item in enumerate(db.get_run_items(rid)):
        metrics = METRICS_BAD if (bad and idx == 0) else METRICS_OK
        db.update_item(item['id'], status='done', metrics=metrics)
    db.finish_run(rid, 'finished', 'report', 'log')
    return rid


class ExportTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        self.client = flask_app.test_client()
        self.auth = patch.object(app_mod, '_session_user', return_value='tester')
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_run_csv_and_json(self):
        rid = make_run(bad=True)
        csv_resp = self.client.get(f'/api/runs/{rid}/export.csv')
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn('text/csv', csv_resp.headers['Content-Type'])
        self.assertIn(f'linkpilot-run-{rid}.csv', csv_resp.headers['Content-Disposition'])
        text = csv_resp.get_data(as_text=True)
        self.assertTrue(text.startswith('\ufeff'), 'CSV 应带 BOM，Excel 打开中文才不乱码')
        rows = [line for line in text.lstrip('\ufeff').splitlines() if line.strip()]
        self.assertIn('上行Mbit/s', rows[0])
        self.assertEqual(len(rows), 1 + 2)      # 表头 + 两台机器
        self.assertIn('b-0', text)

        json_resp = self.client.get(f'/api/runs/{rid}/export.json')
        self.assertEqual(json_resp.status_code, 200)
        payload = json_resp.get_json()
        self.assertEqual(payload['run']['id'], rid)
        self.assertEqual(len(payload['items']), 2)
        self.assertIn('summary', payload)
        self.assertEqual(payload['items'][0]['metrics']['loss_pct'], 3.5)

    def test_csv_escapes_formula_cells(self):
        rid = make_run()
        item = db.get_run_items(rid)[0]
        db.get_db().execute('UPDATE run_items SET machine_name=? WHERE id=?', ('=1+1', item['id']))
        db.get_db().commit()
        text = self.client.get(f'/api/runs/{rid}/export.csv').get_data(as_text=True)
        self.assertIn("'=1+1", text)

    def test_schedule_csv(self):
        rid = make_run(bad=True)
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

    def test_export_missing_run_is_404(self):
        self.assertEqual(self.client.get('/api/runs/999999/export.csv').status_code, 404)
        self.assertEqual(self.client.get('/api/runs/999999/export.json').status_code, 404)


class StreamTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        self.client = flask_app.test_client()
        self.auth = patch.object(app_mod, '_session_user', return_value='tester')
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_digest_changes_with_data(self):
        first = app_mod.compute_state_digest()
        self.assertIn('machines', first)
        make_run()
        second = app_mod.compute_state_digest()
        self.assertNotEqual(first['runs'], second['runs'])
        self.assertNotEqual(first['machines'], second['machines'])

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
        self.assertIn('retry: 3000', joined)
        self.assertIn('event: state', joined)
        for key in ('"machines"', '"runs"', '"schedules"'):
            self.assertIn(key, joined)

    def test_unauthenticated_stream_is_401(self):
        with patch.object(app_mod, '_session_user', return_value=''):
            resp = self.client.get('/api/stream')
        self.assertEqual(resp.status_code, 401)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        _data.cleanup()
