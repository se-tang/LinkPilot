"""Regression checks for databases upgraded from free-form cmd jobs."""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_data = tempfile.TemporaryDirectory(prefix='linkpilot-job-upgrade-')
os.environ['DATA_DIR'] = _data.name
from app import db


class JobUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.get_db = patch.object(db, 'get_db', return_value=self.conn)
        self.get_db.start()

    def tearDown(self):
        self.get_db.stop()
        self.conn.close()

    def legacy(self):
        schema = db.SCHEMA.replace("kind TEXT NOT NULL DEFAULT '',", 'cmd TEXT NOT NULL,')
        schema = schema.replace("    params TEXT NOT NULL DEFAULT '{}',\n", '')
        self.conn.executescript(schema)
        self.conn.execute("INSERT INTO jobs(id,machine_id,cmd,status,output) "
                          "VALUES (42,1,'old command','finished','historical output')")
        self.conn.commit()
        db.init_db()

    def test_original_failure_reproduced(self):
        self.legacy()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'NOT NULL constraint failed: jobs.cmd'):
            self.conn.execute(
                'INSERT INTO jobs (id,machine_id,kind,params,timeout) VALUES (?,?,?,?,?)',
                (43,1,'ensure_tools','{}',600))
        self.conn.rollback()

    def test_old_database_upgrade_and_repeat_init(self):
        self.legacy()
        before = dict(db.get_job(42))
        for _ in range(2):
            db.init_db()
            jid = db.create_job(1, 'ensure_tools', {}, 600)
            job = db.next_queued_job(1)
            self.assertEqual(job['id'], jid)
            self.assertEqual(job['cmd'], '')
            self.assertEqual(job['kind'], 'ensure_tools')
            self.assertEqual(job['params'], '{}')
            db.finish_job(jid, 0)
        self.assertEqual(dict(db.get_job(42)), before)

    def test_new_database_and_clock_rollback(self):
        self.conn.executescript(db.SCHEMA)
        first = db.create_job(1, 'ensure_tools', {}, 600)
        with patch.object(db.time, 'time', return_value=1):
            second = db.create_job(1, 'ensure_tools', {}, 600)
        self.assertGreater(second, first)

    def test_primary_key_collision_retries(self):
        self.conn.executescript(db.SCHEMA)
        first = db.create_job(1, 'ensure_tools', {}, 600)
        with patch.object(db, '_alloc_job_id', side_effect=[first, first + 1]) as alloc:
            self.assertEqual(db.create_job(1, 'ensure_tools', {}, 600), first + 1)
            self.assertEqual(alloc.call_count, 2)

    def test_other_constraints_are_not_masked(self):
        self.conn.executescript(db.SCHEMA)
        with patch.object(db, '_alloc_job_id', wraps=db._alloc_job_id) as alloc:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'NOT NULL constraint failed: jobs.machine_id'):
                db.create_job(None, 'ensure_tools', {}, 600)
            self.assertEqual(alloc.call_count, 1)
        self.assertFalse(self.conn.in_transaction)
        self.assertIsInstance(db.create_job(1, 'ensure_tools', {}, 600), int)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        _data.cleanup()
