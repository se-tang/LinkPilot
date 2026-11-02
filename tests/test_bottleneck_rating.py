"""瓶颈评分及历史页面/导出/长期对比回归；不运行真实网络测速。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_data = tempfile.TemporaryDirectory(prefix='linkpilot-rating-')
os.environ['DATA_DIR'] = _data.name
from app import db, quality, scheduler
from app.app import app


class BottleneckRatingTest(unittest.TestCase):
    def metrics(self, up=298, down=299, loss=0):
        return dict(up_mbits=up, down_mbits=down, loss_pct=loss,
                    rating_code='C', rating_summary='上下行吞吐偏低')

    def test_real_solid_ppflight_sample(self):
        result = quality.evaluate(self.metrics(), {'machine_bandwidth': '10G'},
                                  {'target_bandwidth': '300M'})
        self.assertEqual(result['rating_code'], 'S')
        self.assertEqual(result['rating_summary'], '表现正常')
        self.assertEqual(result['throughput_baseline_mbps'], 300)
        self.assertIn('后端标称 10000M、目标标称 300M', result['rating_detail'])
        self.assertIn('99.3%', result['rating_detail'])
        self.assertIn('99.7%', result['rating_detail'])

    def test_lower_bandwidth_at_either_end(self):
        for backend, target in [('10G', '200M'), ('200M', '10G')]:
            result = quality.evaluate(self.metrics(199, 199), {'bandwidth': backend},
                                      {'target_bandwidth': target})
            self.assertEqual(result['rating_code'], 'S')
            self.assertEqual(result['throughput_baseline_mbps'], 200)

    def test_low_throughput_and_packet_loss_still_count(self):
        result = quality.evaluate(self.metrics(60, 50), {'bandwidth': '10G'},
                                  {'target_bandwidth': '300M'})
        self.assertEqual(result['rating_code'], 'C')
        result = quality.evaluate(self.metrics(298, 299, 5), {'bandwidth': '10G'},
                                  {'target_bandwidth': '300M'})
        self.assertNotEqual(result['rating_code'], 'S')
        self.assertIn('存在丢包', result['rating_tags'])
        result = quality.evaluate(self.metrics(down=None), {'bandwidth': '10G'},
                                  {'target_bandwidth': '300M'})
        self.assertEqual(result['rating_code'], '?')

    def test_unknown_or_zero_bandwidth_fallback(self):
        for backend, target in [('不限', '200M'), ('200M', ''), ('0M', '200M')]:
            result = quality.evaluate(self.metrics(199, 199), {'bandwidth': backend},
                                      {'target_bandwidth': target})
            self.assertEqual(result['rating_code'], 'S')
            self.assertIn('另一端带宽未知', result['rating_detail'])
        result = quality.evaluate(self.metrics(500, 500), {'bandwidth': '不限'}, {})
        self.assertEqual(result['rating_code'], 'S')
        self.assertIsNone(result['throughput_baseline_mbps'])
        self.assertIn('绝对吞吐', result['rating_detail'])

    def test_snapshot_preferred_to_live_machine_field(self):
        result = quality.evaluate(self.metrics(),
                                  {'machine_bandwidth': '300M', 'bandwidth': '10G'},
                                  {'target_bandwidth': '10G'})
        self.assertEqual(result['rating_code'], 'S')

    def test_history_uses_snapshots_without_writing_database(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        with patch.object(db, 'get_db', return_value=conn):
            db.init_db()
            target = db.create_machine(dict(name='target', role='target', region='', bandwidth='300M'))
            backend = db.create_machine(dict(name='backend', role='backend', region='', bandwidth='10G'))
            schedule = db.create_schedule(dict(scheduler.normalize_schedule(dict(
                target_id=target['id'], backend_ids=[backend['id']], name='history',
                interval_seconds=600)), next_run_at=scheduler.compute_next(600)))
            rid = db.create_run(target, [backend['id']], 4, target_host='192.0.2.1',
                                source='schedule', schedule_id=schedule['id'])
            iid = db.get_run_items(rid)[0]['id']
            raw = json.dumps(self.metrics())
            db.update_item(iid, status='done', metrics=raw, up_raw='original output')
            db.update_run(rid, status='finished')
            # 修改当前机器标记不应改变开测时的带宽快照。
            conn.execute("UPDATE machines SET bandwidth='20G'")
            conn.commit()
            items = db.get_run_items(rid)
            self.assertEqual(items[0]['metrics_obj']['rating_code'], 'S')
            self.assertEqual(items[0]['up_raw'], 'original output')
            self.assertEqual(quality.run_summary(db.get_run(rid), items)['attention_count'], 0)
            window = db.schedule_run_window(schedule['id'])
            self.assertEqual(window[0]['items'][0]['metrics_obj']['rating_code'], 'S')
            comparison = scheduler.comparison_data(schedule['id'])
            self.assertEqual(comparison['machines'][0]['series'][0]['rating'], 'S')
            client = app.test_client()
            user, _ = db.get_auth()
            password = Path(db.INITIAL_PW_PATH).read_text(encoding='utf-8').strip()
            login = client.post('/api/login', json={'user': user, 'password': password})
            self.assertEqual(login.status_code, 200)
            detail_response = client.get(f'/api/runs/{rid}')
            self.assertEqual(detail_response.status_code, 200)
            detail = detail_response.get_json()
            self.assertEqual(detail['items'][0]['metrics_obj']['rating_code'], 'S')
            self.assertIn('S 优秀', detail['run']['report'])
            report = client.get(f'/api/runs/{rid}/report')
            self.assertEqual(report.status_code, 200)
            self.assertIn('S 优秀', report.get_data(as_text=True))
            stored = conn.execute('SELECT metrics,up_raw FROM run_items WHERE id=?', (iid,)).fetchone()
            self.assertEqual(stored['metrics'], raw)
            self.assertEqual(stored['up_raw'], 'original output')

    def test_standalone_report_regrades_old_metrics(self):
        item = dict(machine_name='backend', machine_region='', machine_bandwidth='10G',
                    status='done', metrics=json.dumps(self.metrics()))
        report = quality.build_report({}, {'bandwidth': '300M'}, [item])
        self.assertIn('S 优秀', report)
        self.assertNotIn('上下行吞吐偏低', report)
        self.assertEqual(json.loads(item['metrics'])['rating_code'], 'C')

    def test_failed_and_pending_items_are_not_regraded(self):
        for status in ['failed', 'pending', 'running', 'stopped']:
            item = dict(status=status, metrics=json.dumps(self.metrics()))
            before = dict(item)
            self.assertEqual(quality.refresh_item_rating(item, {'target_bandwidth': '300M'}), before)


if __name__ == '__main__':
    unittest.main()
