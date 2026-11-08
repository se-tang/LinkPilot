"""回归：历史保留按来源分开、任务取消、批量计数与忙碌心跳。

直接运行: python tests/test_data_retention.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_data = tempfile.TemporaryDirectory(prefix='linkpilot-retention-')
os.environ['DATA_DIR'] = _data.name

import app.app as app_mod  # noqa: E402
from app import db  # noqa: E402
from app.app import app as flask_app  # noqa: E402

METRICS = json.dumps({'up_mbits': 100.0, 'down_mbits': 90.0}, ensure_ascii=False)


def make_machine(name, role='backend'):
    return db.create_machine({'name': name, 'role': role, 'region': '', 'bandwidth': ''})


def make_run(target, backends, schedule_id=None):
    rid = db.create_run(
        target, [m['id'] for m in backends], 4, target_host='1.2.3.4',
        source='schedule' if schedule_id else 'manual', schedule_id=schedule_id)
    for item in db.get_run_items(rid):
        db.update_item(item['id'], status='done', metrics=METRICS)
    db.finish_run(rid, 'finished', 'report', 'log')
    return rid


class RetentionTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        self.target = make_machine('t-target', 'target')
        self.backends = [make_machine('b-1'), make_machine('b-2')]

    def test_prune_keeps_schedule_history(self):
        schedule = db.create_schedule({
            'name': 'longrun',
            'target_id': self.target['id'],
            'backend_ids': [m['id'] for m in self.backends],
            'interval_seconds': 300,
            'lifetime_seconds': 0,
        })
        sched_runs = [make_run(self.target, self.backends, schedule['id']) for _ in range(12)]
        manual_runs = [make_run(self.target, self.backends) for _ in range(6)]

        os.environ['PANEL_KEEP_SCHEDULE_ROUNDS'] = '5'
        try:
            db.prune_history(keep_runs=3)
        finally:
            del os.environ['PANEL_KEEP_SCHEDULE_ROUNDS']

        rows = db.get_runs(limit=500)
        manual_left = sorted(r['id'] for r in rows if r['schedule_id'] is None)
        sched_left = sorted(r['id'] for r in rows if r['schedule_id'] == schedule['id'])
        # 手动测试按 keep_runs 收敛
        self.assertEqual(manual_left, sorted(manual_runs[-3:]))
        # 定时任务按自己的上限保留，没有被手动测试的上限连带删掉
        self.assertEqual(sched_left, sorted(sched_runs[-5:]))
        # 删掉的轮次连明细一起清掉，避免留下孤儿行
        self.assertIsNone(db.get_run(manual_runs[0]))
        self.assertEqual(db.get_run_items(manual_runs[0]), [])
        # 保留下来的轮次明细还在
        self.assertTrue(db.get_run_items(sched_runs[-1]))

    def test_prune_is_idempotent(self):
        for _ in range(4):
            make_run(self.target, self.backends)
        db.prune_history(keep_runs=2)
        first = sorted(r['id'] for r in db.get_runs(limit=100))
        db.prune_history(keep_runs=2)
        second = sorted(r['id'] for r in db.get_runs(limit=100))
        self.assertEqual(first, second)
        self.assertEqual(len(second), 2)

    def test_bulk_counts_match_single(self):
        rids = [make_run(self.target, self.backends) for _ in range(3)]
        bulk = db.run_counts_bulk(rids)
        for rid in rids:
            self.assertEqual(bulk[rid], db.run_counts(rid))
        self.assertEqual(db.run_counts_bulk([]), {})

    def test_api_runs_avoids_per_run_query(self):
        rid = make_run(self.target, self.backends)
        client = flask_app.test_client()
        with patch.object(app_mod, '_session_user', return_value='tester'):
            with patch.object(db, 'run_counts', side_effect=AssertionError('N+1 查询')):
                resp = client.get('/api/runs')
        self.assertEqual(resp.status_code, 200)
        row = [r for r in resp.get_json() if r['id'] == rid][0]
        self.assertEqual((row['backend_total'], row['done_count'], row['fail_count']), (2, 2, 0))


class CancelTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        self.machine = make_machine('hb-machine')
        self.client = flask_app.test_client()
        self.headers = {'X-Agent-Token': self.machine['token'], 'X-Agent-Protocol': '2'}

    def _heartbeat(self, busy=False):
        headers = dict(self.headers)
        if busy:
            headers['X-Agent-Busy'] = '1'
        return self.client.post('/api/agent/heartbeat', headers=headers).get_data(as_text=True)

    def test_busy_heartbeat_never_dispatches(self):
        jid = db.create_job(self.machine['id'], 'ping', {'host': '1.2.3.4', 'count': 5}, 60)
        body = self._heartbeat(busy=True)
        self.assertIn('status=ok', body)
        self.assertNotIn('job_id=', body)
        self.assertEqual(db.get_job(jid)['status'], 'queued')
        # 不忙的时候才派发
        self.assertIn(f'job_id={jid}', self._heartbeat())

    def test_cancel_running_job_is_reported_to_agent(self):
        jid = db.create_job(self.machine['id'], 'ping', {'host': '1.2.3.4', 'count': 5}, 60)
        db.next_queued_job(self.machine['id'])
        self.assertEqual(db.request_job_cancel([jid]), 1)
        self.assertTrue(db.job_cancel_requested(jid))
        self.assertIn(f'cancel_job={jid}', self._heartbeat(busy=True))
        # 非忙碌心跳只回取消指令，不再派新任务
        body = self._heartbeat(busy=True)
        self.assertNotIn('payload_b64=', body)

    def test_cancel_queued_job_fails_it_immediately(self):
        jid = db.create_job(self.machine['id'], 'ping', {'host': '5.6.7.8', 'count': 1}, 30)
        db.request_job_cancel([jid])
        job = db.get_job(jid)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['exit_code'], -1)
        self.assertIn('任务已取消', job['output'])

    def test_stop_endpoint_cancels_running_job(self):
        # 快照字段取自传入的机器字典，这里不关心角色，只要有一条 run 挂在目标机上
        rid = db.create_run(self.machine, [self.machine['id']], 4, target_host='1.2.3.4')
        jid = db.create_job(self.machine['id'], 'ping', {'host': '1.2.3.4', 'count': 5}, 60)
        db.next_queued_job(self.machine['id'])
        with patch.object(app_mod.runner, 'stop_run', return_value=True), \
                patch.object(app_mod, '_session_user', return_value='tester'):
            resp = self.client.post(f'/api/runs/{rid}/stop')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['cancelled_jobs'], 1)
        self.assertTrue(db.job_cancel_requested(jid))


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        _data.cleanup()
