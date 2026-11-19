"""回归：日常链路监测（多任务、双模式）——任务 CRUD、探测计划、样本、聚合与状态。

直接运行: python tests/test_watch_geo.py
"""
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_data = tempfile.TemporaryDirectory(prefix='linkpilot-watch-')
os.environ['DATA_DIR'] = _data.name

from app import db  # noqa: E402
from app.app import app as flask_app  # noqa: E402
from app.app import _job_sig  # noqa: E402


def wipe():
    """所有测试类共用一个临时库：每个用例前清空相关表，避免相互串扰。"""
    conn = db.get_db()
    for table in ('machines', 'runs', 'run_items', 'jobs', 'ping_watch',
                  'ping_watch_minute', 'watch_tasks', 'settings',
                  'schedules', 'schedule_runs', 'agent_tombstones'):
        conn.execute(f'DELETE FROM {table}')
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                 "('machines', 'watch_tasks', 'runs')")
    conn.commit()


def make_machine(name, role='backend', region='香港', isp='阿里云', lat='', lng=''):
    return db.create_machine({'name': name, 'role': role, 'region': region,
                              'bandwidth': '500M', 'isp': isp, 'lat': lat, 'lng': lng})


def set_ip(machine, ip):
    conn = db.get_db()
    conn.execute('UPDATE machines SET ip4=? WHERE id=?', (ip, machine['id']))
    conn.commit()


def make_run(target, backends, rtt=33.3):
    rid = db.create_run(target, [m['id'] for m in backends], 4, target_host='1.2.3.4')
    metrics = json.dumps({'up_mbits': 100.0, 'down_mbits': 90.0,
                          'rtt_avg': rtt, 'mdev_ms': 1.2, 'loss_pct': 0.0})
    for item in db.get_run_items(rid):
        db.update_item(item['id'], status='done', metrics=metrics)
    db.finish_run(rid, 'finished', 'report', 'log')
    return rid


def watch_line(ts, task_id, dest_id, mode, rtt, mdev, sent, recv):
    fmt = lambda v: '-' if v is None else str(v)
    return f'W {ts} {task_id} {dest_id} {mode} {fmt(rtt)} {fmt(mdev)} {sent} {recv}'


class WatchTaskTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        wipe()
        self.target = make_machine('t1', 'target', '东京')
        self.backends = [make_machine('b1'), make_machine('b2')]

    def test_create_validation(self):
        with self.assertRaises(ValueError):
            db.watch_task_create({'name': '', 'mode': 'A', 'backend_ids': [1],
                                  'target_ids': [self.target['id']]})
        with self.assertRaises(ValueError):
            db.watch_task_create({'name': 'x', 'mode': 'A', 'backend_ids': [],
                                  'target_ids': [self.target['id']]})
        with self.assertRaises(ValueError):
            db.watch_task_create({'name': 'x', 'mode': 'A', 'backend_ids': [1],
                                  'target_ids': []})
        with self.assertRaises(ValueError):
            db.watch_task_create({'name': 'x', 'mode': 'A', 'backend_ids': [1],
                                  'target_ids': [self.target['id']], 'interval': 2})
        with self.assertRaises(ValueError):
            # 模式 A 的目标必须是 target 角色
            db.watch_task_create({'name': 'x', 'mode': 'A', 'backend_ids': [1],
                                  'target_ids': [2]})

    def test_create_and_defaults(self):
        task, conflicts = db.watch_task_create({
            'name': '亚洲监控', 'mode': 'A',
            'backend_ids': [m['id'] for m in self.backends],
            'target_ids': [self.target['id']], 'interval': 10})
        self.assertEqual(task['status'], 'running')
        self.assertEqual(task['mode'], 'A')
        self.assertEqual(task['interval_seconds'], 10)
        self.assertEqual(task['probe_count'], 2)
        self.assertEqual(conflicts, [])
        tasks = db.watch_tasks_list()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['last_report_at'], None)

    def test_mode_b_clears_targets(self):
        task, _ = db.watch_task_create({
            'name': '面板可达', 'mode': 'B',
            'backend_ids': [m['id'] for m in self.backends],
            'target_ids': [self.target['id']]})   # 模式 B 下目标被忽略
        self.assertEqual(task['target_ids'], [])

    def test_conflict_detection(self):
        db.watch_task_create({'name': 'A1', 'mode': 'A', 'backend_ids': [1],
                              'target_ids': [self.target['id']]})
        _, conflicts = db.watch_task_create({
            'name': 'A2', 'mode': 'A', 'backend_ids': [1, 2],
            'target_ids': [self.target['id']]})
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['task'], 'A1')
        # 不同目标不冲突
        other_target = make_machine('t2', 'target', '新加坡')
        _, conflicts = db.watch_task_create({
            'name': 'A3', 'mode': 'A', 'backend_ids': [1], 'target_ids': [other_target['id']]})
        self.assertEqual(conflicts, [])

    def test_update_partial(self):
        task, _ = db.watch_task_create({'name': 'T', 'mode': 'A', 'backend_ids': [1],
                                        'target_ids': [self.target['id']], 'interval': 10})
        updated, _ = db.watch_task_update(task['id'], {'interval': 30})
        self.assertEqual(updated['interval_seconds'], 30)
        self.assertEqual(updated['name'], 'T')
        self.assertEqual(updated['backend_ids'], [1])

    def test_actions_and_delete(self):
        task, _ = db.watch_task_create({'name': 'T', 'mode': 'B', 'backend_ids': [1]})
        tid = task['id']
        self.assertEqual(db.watch_task_action(tid, 'pause')['status'], 'paused')
        self.assertEqual(db.watch_task_action(tid, 'resume')['status'], 'running')
        self.assertEqual(db.watch_task_action(tid, 'stop')['status'], 'stopped')
        self.assertEqual(db.watch_task_action(tid, 'start')['status'], 'running')
        # 插入样本后删除：默认保留历史，purge 才清理
        now = int(time.time())
        db.insert_watch_samples(1, watch_line(now, tid, 0, 'B', 10, 1, 4, 4))
        db.watch_task_delete(tid)
        self.assertEqual(db.watch_task_get(tid)['archived'], 1)
        self.assertEqual(db.watch_tasks_list(), [])
        conn = db.get_db()
        self.assertEqual(conn.execute('SELECT COUNT(*) c FROM ping_watch').fetchone()['c'], 1)
        # 重建一个同 id 不可能，但直接验证 purge 语义：删除别的任务带 purge
        task2, _ = db.watch_task_create({'name': 'T2', 'mode': 'B', 'backend_ids': [2]})
        db.watch_task_delete(task2['id'], purge=True)
        self.assertEqual(conn.execute('SELECT COUNT(*) c FROM ping_watch').fetchone()['c'], 1)


class WatchPlanTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        wipe()
        self.t_tokyo = make_machine('t1', 'target', '东京')
        set_ip(self.t_tokyo, '203.0.113.10')
        self.t_sg = make_machine('t2', 'target', '新加坡')
        set_ip(self.t_sg, '203.0.113.11')
        self.b1 = make_machine('b1')

    def test_plan_respects_membership_and_status(self):
        db.watch_task_create({'name': 'A1', 'mode': 'A', 'backend_ids': [self.b1['id']],
                              'target_ids': [self.t_tokyo['id'], self.t_sg['id']],
                              'interval': 10})
        plan = db.agent_watch_plan(self.b1['id'], panel_host='panel.example.com')
        self.assertEqual([(p['dest_id'], p['host'], p['interval']) for p in plan],
                         [(self.t_tokyo['id'], '203.0.113.10', 10),
                          (self.t_sg['id'], '203.0.113.11', 10)])
        # 非参与机器没有计划
        other = make_machine('b2')
        self.assertEqual(db.agent_watch_plan(other['id']), [])
        # 暂停后计划清空
        tasks = db.watch_tasks_list()
        db.watch_task_action(tasks[0]['id'], 'pause')
        self.assertEqual(db.agent_watch_plan(self.b1['id']), [])

    def test_plan_mode_b_uses_panel_host(self):
        db.watch_task_create({'name': 'B1', 'mode': 'B', 'backend_ids': [self.b1['id']],
                              'interval': 30})
        plan = db.agent_watch_plan(self.b1['id'], panel_host='panel.example.com')
        self.assertEqual(plan, [{'task_id': plan[0]['task_id'], 'mode': 'B', 'interval': 30,
                                 'dest_id': 0, 'host': 'panel.example.com'}])

    def test_multiple_tasks_same_machine(self):
        db.watch_task_create({'name': 'A1', 'mode': 'A', 'backend_ids': [self.b1['id']],
                              'target_ids': [self.t_tokyo['id']], 'interval': 10})
        db.watch_task_create({'name': 'B1', 'mode': 'B', 'backend_ids': [self.b1['id']],
                              'interval': 30})
        plan = db.agent_watch_plan(self.b1['id'], panel_host='panel.example.com')
        self.assertEqual(len(plan), 2)
        self.assertEqual({p['mode'] for p in plan}, {'A', 'B'})
        self.assertEqual({p['interval'] for p in plan}, {10, 30})


class WatchSamplesTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        wipe()
        self.target = make_machine('t1', 'target', '东京')
        set_ip(self.target, '203.0.113.10')
        self.m = make_machine('b1')
        self.task, _ = db.watch_task_create({
            'name': 'T', 'mode': 'A', 'backend_ids': [self.m['id']],
            'target_ids': [self.target['id']], 'interval': 10})
        self.tid = self.task['id']
        self.did = self.target['id']

    def test_parse_and_view(self):
        now = int(time.time())
        lines = '\n'.join([
            watch_line(now - 40, self.tid, self.did, 'A', 30.0, 1.0, 4, 4),
            watch_line(now - 20, self.tid, self.did, 'A', 32.0, 1.2, 4, 3),   # 25% 丢包
            watch_line(now, self.tid, self.did, 'A', None, None, 4, 0),       # 整轮超时
            watch_line(now, 999, self.did, 'A', 10, 1, 4, 4),                 # 未知任务丢弃
            watch_line(now, self.tid, self.did, 'A', 10, 1, 4, 9),            # recv>sent 拒绝
            'garbage line',
        ])
        n = db.insert_watch_samples(self.m['id'], lines)
        self.assertEqual(n, 3)
        view = db.watch_view('A')
        self.assertEqual(len(view['links']), 1)
        l = view['links'][0]
        self.assertEqual(l['task_id'], self.tid)
        self.assertEqual(l['dest_name'], 't1')
        self.assertIsNone(l['rtt'])                    # 最近一轮超时
        self.assertAlmostEqual(l['avg'], 31.0)         # 均值只算有效轮
        self.assertAlmostEqual(l['loss'], (1 - 7 / 12) * 100)  # 按收发数加权
        self.assertEqual(l['status'], 'warn')          # 有丢包但未连续全超时
        self.assertEqual(len(l['spark']), 3)

    def test_status_ok_warn_down_stale(self):
        now = int(time.time())
        # 全部正常 → ok
        db.insert_watch_samples(self.m['id'], '\n'.join(
            watch_line(now - i * 10, self.tid, self.did, 'A', 30 + i, 1, 4, 4)
            for i in range(3)))
        self.assertEqual(db.watch_view('A')['links'][0]['status'], 'ok')
        # 清空后：最近一轮有丢包 → warn
        db.get_db().execute('DELETE FROM ping_watch')
        db.get_db().commit()
        db.insert_watch_samples(self.m['id'], '\n'.join([
            watch_line(now, self.tid, self.did, 'A', 30, 1, 4, 3)]))
        self.assertEqual(db.watch_view('A')['links'][0]['status'], 'warn')
        # 末尾连续全超时（但数据还新鲜）→ down；机器从未上线不影响判定
        db.get_db().execute('DELETE FROM ping_watch')
        db.get_db().commit()
        db.insert_watch_samples(self.m['id'], '\n'.join([
            watch_line(now - 30, self.tid, self.did, 'A', 30, 1, 4, 4),
            watch_line(now - 10, self.tid, self.did, 'A', None, None, 4, 0),
            watch_line(now, self.tid, self.did, 'A', None, None, 4, 0)]))
        self.assertEqual(db.watch_view('A')['links'][0]['status'], 'down')
        # 数据久无更新 → 数据过期（即使机器在线与否）
        db.get_db().execute('DELETE FROM ping_watch')
        db.get_db().commit()
        db.insert_watch_samples(self.m['id'],
                                watch_line(now - 600, self.tid, self.did, 'A', 30, 1, 4, 4))
        self.assertEqual(db.watch_view('A')['links'][0]['status'], 'stale')

    def test_task_filter_and_mode_isolation(self):
        now = int(time.time())
        db.insert_watch_samples(self.m['id'], watch_line(now, self.tid, self.did, 'A', 30, 1, 4, 4))
        # 同机器同目标，另一个任务没有数据
        task2, _ = db.watch_task_create({
            'name': 'T2', 'mode': 'A', 'backend_ids': [self.m['id']],
            'target_ids': [self.target['id']]})
        all_view = db.watch_view('A')
        self.assertEqual(len(all_view['links']), 1)      # 无样本的任务不出链路
        one_view = db.watch_view('A', task2['id'])
        self.assertEqual(one_view['links'], [])
        self.assertEqual([t['id'] for t in one_view['tasks']], [task2['id']])
        # 模式不匹配的样本直接被拒：A 任务收到的 B 样本不入库
        n = db.insert_watch_samples(self.m['id'],
                                    watch_line(now, self.tid, 0, 'B', 15, 1, 4, 4))
        self.assertEqual(n, 0)
        self.assertEqual(len(db.watch_view('A')['links']), 1)
        # 真正的模式 B 任务：数据独立、互不混合
        tb, _ = db.watch_task_create({'name': 'B1', 'mode': 'B',
                                      'backend_ids': [self.m['id']]})
        db.insert_watch_samples(self.m['id'], watch_line(now, tb['id'], 0, 'B', 15, 1, 4, 4))
        b_view = db.watch_view('B')
        self.assertEqual(len(b_view['links']), 1)
        self.assertEqual(b_view['links'][0]['dest_id'], 0)
        self.assertEqual(b_view['links'][0]['task_id'], tb['id'])


class RollupTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        wipe()
        self.m = make_machine('b1')
        self.task, _ = db.watch_task_create({'name': 'T', 'mode': 'B',
                                             'backend_ids': [self.m['id']]})
        self.tid = self.task['id']

    def test_rollup_and_history(self):
        conn = db.get_db()
        now = int(time.time())
        old = (now - db.WATCH_RAW_RETENTION - 600) // 60 * 60   # 对齐分钟桶，避免跨桶 flaky
        rows = []
        for i in range(6):      # 6 轮老样本（同一分钟）
            rows.append((self.tid, self.m['id'], 0, 'B', 10.0 + i, 1.0, 4, 4 if i < 5 else 0,
                         old + i))
        conn.executemany(
            'INSERT INTO ping_watch (task_id, machine_id, dest_id, mode, rtt, jit, sent, recv, ts) '
            'VALUES (?,?,?,?,?,?,?,?,?)', rows)
        # 1 条新鲜样本（留在 raw，供 1h 趋势）
        conn.execute(
            'INSERT INTO ping_watch (task_id, machine_id, dest_id, mode, rtt, jit, sent, recv, ts) '
            'VALUES (?,?,?,?,?,?,?,?,?)', (self.tid, self.m['id'], 0, 'B', 20.0, 1.0, 4, 4, now))
        conn.commit()
        db.rollup_watch_raw()
        # 原始表只剩新鲜样本；老样本进分钟表
        self.assertEqual(conn.execute('SELECT COUNT(*) c FROM ping_watch').fetchone()['c'], 1)
        agg = conn.execute('SELECT * FROM ping_watch_minute').fetchone()
        self.assertEqual(agg['sent'], 24)
        self.assertEqual(agg['recv'], 20)
        self.assertEqual(agg['samples'], 6)
        self.assertEqual(agg['timeout'], 0)
        self.assertAlmostEqual(agg['rtt_min'], 10.0)
        self.assertAlmostEqual(agg['rtt_max'], 15.0)
        # 历史：24h 走分钟表，1h 走原始表
        h1d = db.watch_history(self.tid, self.m['id'], 0, 'B', '1d')
        self.assertEqual(len(h1d['points']), 1)
        p = h1d['points'][0]
        self.assertAlmostEqual(p['rtt'], 12.5)   # (10+..+15)/6
        self.assertAlmostEqual(p['loss'], (1 - 20 / 24) * 100)
        h1 = db.watch_history(self.tid, self.m['id'], 0, 'B', '1h')
        self.assertEqual(len(h1['points']), 1)
        self.assertAlmostEqual(h1['points'][0]['rtt'], 20.0)

    def test_minute_retention(self):
        conn = db.get_db()
        old_bucket = int(time.time()) - db.WATCH_MINUTE_RETENTION - 3600
        conn.execute(
            'INSERT INTO ping_watch_minute (task_id, machine_id, dest_id, mode, bucket, sent, recv, '
            'rtt_sum, samples) VALUES (?,?,?,?,?,?,?,?,?)',
            (self.tid, self.m['id'], 0, 'B', old_bucket, 4, 4, 40.0, 1))
        db.rollup_watch_raw()
        self.assertEqual(conn.execute('SELECT COUNT(*) c FROM ping_watch_minute').fetchone()['c'], 0)


class GeoApiTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        wipe()
        self.target = make_machine('t-target', 'target', '东京')
        set_ip(self.target, '203.0.113.10')
        self.backend = make_machine('b-api')
        self.client = flask_app.test_client()

    def agent_headers(self, machine, **extra):
        h = {'X-Agent-Token': machine['token'], 'X-Agent-Protocol': '2'}
        h.update(extra)
        return h

    def admin(self):
        return patch('app.app._session_user', return_value='tester')

    def test_machine_geo_fields(self):
        with self.admin(), self.client as c:
            resp = c.post('/api/machines', json={
                'name': 'm1', 'role': 'backend', 'region': '香港',
                'isp': '阿里云', 'lat': '22.32', 'lng': '114.17'})
            self.assertEqual(resp.status_code, 200)
            m = resp.get_json()
            self.assertEqual(m['isp'], '阿里云')
            self.assertEqual(m['lat'], '22.32')
            resp = c.post('/api/machines', json={
                'name': 'm2', 'role': 'backend', 'region': '', 'lat': 'bad'})
            self.assertEqual(resp.status_code, 400)

    def test_task_crud_api(self):
        with self.admin(), self.client as c:
            resp = c.post('/api/watch/tasks', json={
                'name': '香港业务', 'mode': 'A', 'interval': 10,
                'backend_ids': [self.backend['id']], 'target_ids': [self.target['id']]})
            self.assertEqual(resp.status_code, 200)
            task = resp.get_json()['task']
            self.assertEqual(task['probe_count'], 1)
            # 重复关系提示
            resp = c.post('/api/watch/tasks', json={
                'name': '重复', 'mode': 'A', 'interval': 30,
                'backend_ids': [self.backend['id']], 'target_ids': [self.target['id']]})
            self.assertEqual(len(resp.get_json()['conflicts']), 1)
            # 校验错误
            resp = c.post('/api/watch/tasks', json={'name': 'x', 'mode': 'A',
                                                    'backend_ids': [], 'target_ids': []})
            self.assertEqual(resp.status_code, 400)
            # 启停
            resp = c.post(f"/api/watch/tasks/{task['id']}/action",
                          json={'action': 'pause'})
            self.assertEqual(resp.get_json()['task']['status'], 'paused')
            # 列表
            resp = c.get('/api/watch/tasks')
            self.assertEqual(len(resp.get_json()['tasks']), 2)
            # 删除（保留历史）
            resp = c.delete(f"/api/watch/tasks/{task['id']}")
            self.assertEqual(resp.status_code, 200)
            resp = c.get('/api/watch/tasks')
            self.assertEqual(len(resp.get_json()['tasks']), 1)

    def test_heartbeat_multi_task_cfg(self):
        t1, _ = db.watch_task_create({
            'name': 'A1', 'mode': 'A', 'backend_ids': [self.backend['id']],
            'target_ids': [self.target['id']], 'interval': 10})
        t2, _ = db.watch_task_create({
            'name': 'B1', 'mode': 'B', 'backend_ids': [self.backend['id']], 'interval': 30})
        resp = self.client.post('/api/agent/heartbeat', headers=self.agent_headers(self.backend))
        text = resp.get_data(as_text=True)
        lines = dict(l.split('=', 1) for l in text.strip().splitlines())
        self.assertIn('watch_ver', lines)
        body = base64.b64decode(lines['watch_cfg']).decode()
        self.assertIn(f'mode_{t1["id"]}=A', body)
        self.assertIn(f'dests_{t1["id"]}={self.target["id"]}:203.0.113.10', body)
        self.assertIn(f'mode_{t2["id"]}=B', body)
        self.assertIn(f'dests_{t2["id"]}=0:localhost', body)
        expect = _job_sig(self.backend['sign_key'], f"watch:{lines['watch_ver']}",
                          lines['watch_cfg'])
        self.assertEqual(lines['watch_sig'], expect)
        # 暂停 A1 后配置里不再出现它（ver 变化 → Agent 重启循环）
        db.watch_task_action(t1['id'], 'pause')
        resp = self.client.post('/api/agent/heartbeat', headers=self.agent_headers(self.backend))
        body2 = base64.b64decode(dict(
            l.split('=', 1) for l in resp.get_data(as_text=True).strip().splitlines()
        )['watch_cfg']).decode()
        self.assertNotIn(f'mode_{t1["id"]}=A', body2)
        self.assertIn(f'mode_{t2["id"]}=B', body2)
        # 全部任务移除 → off
        db.watch_task_action(t2['id'], 'stop')
        resp = self.client.post('/api/agent/heartbeat',
                                headers=self.agent_headers(self.backend, **{'X-Agent-Busy': '1'}))
        self.assertIn('watch_ver=off', resp.get_data(as_text=True))

    def test_agent_watch_upload_and_geo_daily(self):
        t1, _ = db.watch_task_create({
            'name': 'B1', 'mode': 'B', 'backend_ids': [self.backend['id']],
            'interval': 10})
        now = int(time.time())
        payload = '\n'.join([
            watch_line(now - 30, t1['id'], 0, 'B', 18.2, 0.6, 4, 4),
            watch_line(now - 20, t1['id'], 0, 'B', 19.4, 0.7, 4, 3),
            watch_line(now, t1['id'], 0, 'B', None, None, 4, 0),
        ])
        resp = self.client.post('/api/agent/watch', headers=self.agent_headers(self.backend),
                                data={'watch': payload})
        self.assertIn('saved=3', resp.get_data(as_text=True))
        resp = self.client.post('/api/agent/watch', headers={'X-Agent-Token': 'bad'},
                                data={'watch': payload})
        self.assertEqual(resp.status_code, 403)

        with self.admin(), self.client as c:
            data = c.get('/api/geo/daily?mode=B').get_json()
            self.assertEqual(data['view']['mode'], 'B')
            self.assertTrue(any(m['id'] == self.backend['id'] for m in data['machines']))
            link = next(l for l in data['links'] if l['machine_id'] == self.backend['id'])
            self.assertEqual(link['dest_id'], 0)
            self.assertIsNone(link['rtt'])                     # 最近一轮超时
            self.assertAlmostEqual(link['avg'], 18.8)
            self.assertAlmostEqual(link['loss'], (1 - 7 / 12) * 100)
            self.assertEqual(link['status'], 'warn')   # 末轮超时但上轮部分成功
            # 任务筛选
            data2 = c.get(f'/api/geo/daily?mode=B&task={t1["id"]}').get_json()
            self.assertEqual(len(data2['links']), 1)
            # 视图切到 A：B 的数据不会混进来
            data3 = c.get('/api/geo/daily?mode=A').get_json()
            self.assertEqual(data3['links'], [])

    def test_geo_test_endpoint(self):
        rid1 = make_run(self.target, [self.backend], rtt=42)
        rid2 = make_run(self.target, [self.backend], rtt=55)
        with self.admin(), self.client as c:
            data = c.get('/api/geo/test').get_json()
            data_old = c.get(f'/api/geo/test?run_id={rid1}').get_json()
        self.assertIsNotNone(data['run'])
        self.assertEqual(data['run']['run_id'], rid2)
        self.assertEqual(data['run']['target_name'], 't-target')
        self.assertAlmostEqual(data['run']['links'][0]['rtt'], 55)
        self.assertEqual(len(data['runs']), 2)
        self.assertEqual(data_old['run']['run_id'], rid1)
        self.assertAlmostEqual(data_old['run']['links'][0]['rtt'], 42)

    def test_watch_settings_api(self):
        with self.admin(), self.client as c:
            resp = c.post('/api/watch/settings', json={'panel_region': '东京', 'count': 6})
            self.assertEqual(resp.get_json(), {'panel_region': '东京', 'count': 6})
            resp = c.post('/api/watch/settings', json={'count': 99})
            self.assertEqual(resp.status_code, 400)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        import contextlib
        with contextlib.suppress(OSError):   # Windows: SQLite 句柄可能仍被占用
            _data.cleanup()
