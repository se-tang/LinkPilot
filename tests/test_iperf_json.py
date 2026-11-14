"""iperf3 `-J` 结构化输出解析：真实样本（来自 v0.9.0 上机测试，IP 已脱敏）。

直接运行: python tests/test_iperf_json.py
"""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DATA_DIR', '/tmp/linkpilot-json-test')

from app import job_types, quality  # noqa: E402

FIXTURES = ROOT / 'tests' / 'fixtures'

PING_RAW = """PING 192.0.2.10 (192.0.2.10) 56(84) bytes of data.
64 bytes from 192.0.2.10: icmp_seq=1 ttl=52 time=144.3 ms

--- 192.0.2.10 ping statistics ---
10 packets transmitted, 10 received, 0% packet loss, time 9000ms
rtt min/avg/max/mdev = 144.239/144.402/146.898/0.255 ms
"""

TEXT_UP = """[ ID] Interval           Transfer     Bitrate         Retr
[  5]   0.00-10.00  sec   163 MBytes   137 Mbits/sec    0            sender
[  5]   0.00-10.15  sec   163 MBytes   135 Mbits/sec                  receiver
"""


def load(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


class IperfJsonTest(unittest.TestCase):
    def test_real_tcp_sample(self):
        sender, receiver, intervals = quality.parse_iperf_any(load('iperf3_json_up.json'))
        self.assertAlmostEqual(sender['mbits'], 19.082251, places=4)
        self.assertAlmostEqual(receiver['mbits'], 19.071675, places=4)
        self.assertEqual(sender['retr'], 15)
        # -O 剔除的那一秒（omitted=true）不计入稳定性样本
        self.assertEqual(len(intervals), 5)

        st = quality.stability(intervals)
        self.assertEqual(st['samples'], 5)
        self.assertAlmostEqual(st['cv_pct'], 4.095359, places=4)
        self.assertAlmostEqual(st['p95_mbits'], 19.928325, places=4)

    def test_parse_metrics_json(self):
        mt = quality.parse_metrics(PING_RAW, load('iperf3_json_up.json'),
                                   load('iperf3_json_down.json'), omit=1)
        self.assertAlmostEqual(mt['up_mbits'], 19.082251, places=4)
        self.assertAlmostEqual(mt['down_mbits'], 18.873621, places=4)
        self.assertEqual(mt['retr_total'], 15 + 39)
        self.assertEqual(mt['omit_seconds'], 1)
        self.assertIsNotNone(mt['up_cv_pct'])
        self.assertIsNotNone(mt['down_cv_pct'])
        self.assertIsNotNone(mt['up_p95_mbits'])
        self.assertEqual(mt['loss_pct'], 0.0)
        self.assertTrue(mt['up_block'].startswith('[ ID] Interval           Transfer     Bitrate         Retr'))
        self.assertIn('[SUM]   0.00-5.00', mt['up_block'])
        self.assertIn('19.1 Mbits/sec', mt['up_block'])
        self.assertNotIn('稳定性', mt['up_block'])
        self.assertNotIn('"intervals"', mt['up_block'])
        # 评级逻辑不受影响
        quality.evaluate(mt, {'machine_bandwidth': '1G'}, {'target_bandwidth': '1G'})
        self.assertIn(mt['rating_code'], ('S', 'A', 'B', 'C'))

    def test_text_fallback_still_works(self):
        mt = quality.parse_metrics(PING_RAW, TEXT_UP, TEXT_UP)
        self.assertEqual(mt['up_mbits'], 137.0)
        self.assertEqual(mt['down_mbits'], 135.0)
        self.assertIsNone(mt['up_cv_pct'])
        self.assertIn('137 Mbits/sec', mt['up_block'])

    def test_udp_json_uses_end_sum(self):
        doc = {
            'start': {'connected': [{'socket': 4}]},
            'end': {
                'sum_sent': {'bits_per_second': 100e6, 'bytes': 60e6, 'sender': True},
                'sum_received': {'bits_per_second': 99e6, 'bytes': 59.4e6},
                'sum': {'jitter_ms': 0.42, 'lost_packets': 7, 'packets': 70000,
                        'lost_percent': 0.01},
            },
            'intervals': [],
        }
        mt = quality.parse_metrics(PING_RAW, json.dumps(doc), json.dumps(doc))
        self.assertTrue(mt['udp'])
        self.assertAlmostEqual(mt['up_udp_loss_pct'], 0.01)
        self.assertAlmostEqual(mt['down_jitter_ms'], 0.42)
        self.assertEqual(mt['up_mbits'], 100.0)
        self.assertEqual(mt['down_mbits'], 99.0)
        self.assertIn('Jitter    Lost/Total Datagrams', mt['up_block'])
        self.assertIn('0.420 ms  7/70000 (0.01%)  receiver', mt['up_block'])
        self.assertNotIn('[SUM]', mt['up_block'])

    def test_stability_edges(self):
        self.assertIsNone(quality.stability([]))
        self.assertIsNone(quality.stability([10.0, 10.0]))
        flat = quality.stability([10.0, 10.0, 10.0, 10.0])
        self.assertEqual(flat['cv_pct'], 0.0)
        self.assertEqual(flat['p95_mbits'], 10.0)
        # 均值 0（全挂）不应产生除零
        self.assertIsNone(quality.stability([0.0, 0.0, 0.0]))

    def test_single_stream_without_intervals_uses_socket_id(self):
        doc = {
            'start': {'connected': [{'socket': 5}]},
            'end': {
                'sum_sent': dict(start=0, end=10, bytes=163 * 1048576,
                                 bits_per_second=137e6, retransmits=0),
                'sum_received': dict(start=0, end=10.15, bytes=163 * 1048576,
                                     bits_per_second=135e6),
            },
        }
        block = quality.parse_metrics(PING_RAW, json.dumps(doc), '', omit=3)['up_block']
        self.assertEqual(block, TEXT_UP.strip())

    def test_job_params_omit_and_json(self):
        clean = job_types.normalize('iperf_client', {
            'host': '192.0.2.10', 'port': 5201, 'duration': 10, 'streams': 1,
            'json': True, 'omit': 3,
        })
        self.assertTrue(clean['json'])
        self.assertEqual(clean['omit'], 3)
        line = job_types.encode('iperf_client', {
            'host': '192.0.2.10', 'port': 5201, 'duration': 10, 'streams': 1,
            'json': True, 'omit': 3,
        })
        self.assertIn('json=1', line)
        self.assertIn('omit=3', line)
        self.assertIn('-O 3 -J', job_types.preview('iperf_client', {
            'host': '192.0.2.10', 'port': 5201, 'duration': 10, 'streams': 1,
            'json': True, 'omit': 3,
        }))
        # 剔除秒数不能吃掉整个测试
        with self.assertRaises(ValueError):
            job_types.normalize('iperf_client', {
                'host': '192.0.2.10', 'port': 5201, 'duration': 5, 'streams': 1, 'omit': 5,
            })


if __name__ == '__main__':
    unittest.main(verbosity=2)
