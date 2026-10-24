"""用样例数据验证解析/评价/报告逻辑。直接运行: python tests/test_sample.py"""
import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='linkpilot-test-')

from app import quality  # noqa: E402

PING_RAW = """PING 1.2.3.4 (1.2.3.4) 56(84) bytes of data.
64 bytes from 1.2.3.4: icmp_seq=1 ttl=52 time=144.3 ms
64 bytes from 1.2.3.4: icmp_seq=2 ttl=52 time=144.4 ms

--- 1.2.3.4 ping statistics ---
200 packets transmitted, 200 received, 0% packet loss, time 199034ms
rtt min/avg/max/mdev = 144.239/144.402/146.898/0.255 ms
"""

UP_RAW = """Connecting to host 1.2.3.4, port 5201
[  5] local 10.0.0.1 port 51234 connected to 1.2.3.4 port 5201
[ ID] Interval           Transfer     Bitrate         Retr  Cwnd
[  5]   0.00-1.00   sec  16.4 MBytes   137 Mbits/sec    0   1.20 MBytes
[  5]   0.00-2.00   sec  32.7 MBytes   137 Mbits/sec    0   1.45 MBytes
- - - - - - - - - - - - - - - - - - - - - - - -
[ ID] Interval           Transfer     Bitrate         Retr
[  5]   0.00-10.00  sec   163 MBytes   137 Mbits/sec    0            sender
[  5]   0.00-10.15  sec   163 MBytes   135 Mbits/sec                  receiver
"""

DOWN_RAW = """Connecting to host 1.2.3.4, port 5201 (reverse mode)
[  5] local 10.0.0.1 port 51236 connected to 1.2.3.4 port 5201
[ ID] Interval           Transfer     Bitrate         Retr  Cwnd
[  5]   0.00-1.00   sec  15.0 MBytes   126 Mbits/sec
[ ID] Interval           Transfer     Bitrate         Retr
[  5]   0.00-10.15  sec   147 MBytes   121 Mbits/sec    0            sender
[  5]   0.00-10.00  sec   129 MBytes   108 Mbits/sec                  receiver
"""


def test_parse():
    mt = quality.parse_metrics(PING_RAW, UP_RAW, DOWN_RAW)
    assert mt['loss_pct'] == 0.0, mt
    assert abs(mt['rtt_avg'] - 144.402) < 1e-6, mt
    assert abs(mt['mdev_ms'] - 0.255) < 1e-6, mt
    assert abs(mt['up_mbits'] - 137.0) < 1e-6, mt
    assert abs(mt['down_mbits'] - 108.0) < 1e-6, mt
    assert mt['retr_total'] == 0, mt
    assert mt['ping_block'] == (
        '200 packets transmitted, 200 received, 0% packet loss, time 199034ms\n'
        'rtt min/avg/max/mdev = 144.239/144.402/146.898/0.255 ms'), mt['ping_block']
    assert '0.00-10.00  sec   163 MBytes   137 Mbits/sec    0            sender' in mt['up_block']
    assert mt['up_block'].count('sender') == 1 and mt['up_block'].count('receiver') == 1
    assert '[ ID] Interval' in mt['up_block'] and '[ ID] Interval' in mt['down_block']
    print('parse_metrics OK:', {k: mt[k] for k in ('up_mbits', 'down_mbits', 'loss_pct', 'rtt_avg', 'mdev_ms', 'retr_total')})
    return mt


def test_units():
    # Gbits / GBytes 单位换算
    us, ur = quality.parse_iperf('[  5]   0.00-10.00  sec   1.19 GBytes   1.02 Gbits/sec    3            sender')
    assert abs(us['mbits'] - 1020.0) < 1e-6
    assert abs(us['transfer_mb'] - 1.19 * 1024) < 1e-6
    assert us['retr'] == 3
    print('unit conversion OK')


def test_evaluate():
    mt = quality.parse_metrics(PING_RAW, UP_RAW, DOWN_RAW)
    quality.evaluate(mt, {'bandwidth': '500M'}, {'duration': 10})
    assert mt['rating_code'] in ('S', 'A', 'B', 'C'), mt
    assert mt['rating'] in ('优秀', '良好', '一般', '较差'), mt
    assert mt['rating_tags'] and mt['rating_diagnostics'], mt
    assert '标称' in mt['rating_detail'], mt
    print('evaluate OK:', mt['rating'], '|', mt['rating_detail'])


def test_rating_system():
    """五级评级、失败/数据不足、异常标签和运行摘要必须来自真实指标。"""
    from app import quality

    good = quality.parse_metrics(PING_RAW, UP_RAW, DOWN_RAW)
    good.update({'loss_pct': 0.0, 'rtt_avg': 30.0, 'mdev_ms': 0.5,
                 'up_mbits': 460.0, 'up_sender_mbits': 470.0,
                 'up_receiver_mbits': 460.0, 'down_mbits': 455.0,
                 'down_sender_mbits': 465.0, 'down_receiver_mbits': 455.0,
                 'retr_total': 0, 'transfer_total_mb': 1200.0})
    quality.evaluate(good, {'bandwidth': '500M'}, {'duration': 10})
    assert good['rating_code'] == 'S', good
    actual_shape = dict(good)
    quality.evaluate(actual_shape, {'machine_bandwidth': '500M'}, {'duration': 10})
    assert '吞吐基线缺失' not in actual_shape['rating_tags'], actual_shape
    no_latency = dict(good)
    no_latency.pop('rtt_avg', None)
    no_latency.pop('mdev_ms', None)
    no_latency['retr_total'] = None
    quality.evaluate(no_latency, {'bandwidth': '500M'}, {'duration': 10})
    assert no_latency['rating_code'] == 'S', no_latency

    poor = dict(good, loss_pct=2.5, rtt_avg=240.0, mdev_ms=18.0,
                up_mbits=120.0, down_mbits=90.0, retr_total=40,
                transfer_total_mb=120.0)
    quality.evaluate(poor, {'bandwidth': '500M'}, {'duration': 10})
    assert poor['rating_code'] in ('B', 'C'), poor
    assert any(t in poor['rating_tags'] for t in ('存在丢包', '重传偏高', '上行吞吐偏低')), poor
    assert poor['rating_summary'] and poor['rating_diagnostics'], poor

    incomplete = dict(good, down_mbits=None)
    quality.evaluate(incomplete, {'bandwidth': '500M'}, {'duration': 10})
    assert incomplete['rating_code'] == '?' and incomplete['rating'] == '数据不足', incomplete

    failed = quality.failure_metrics({'machine_name': 'bad'}, '连接超时', {'duration': 10})
    assert failed['rating_code'] == 'F' and '测试失败' in failed['rating_tags'], failed

    summary = quality.run_summary({'target_name': 'T', 'status': 'finished'}, [
        {'status': 'done', 'metrics_obj': good},
        {'status': 'done', 'metrics_obj': poor},
        {'status': 'failed', 'metrics_obj': failed, 'error': '连接超时'},
    ])
    assert summary['success_count'] == 2 and summary['failure_count'] == 1, summary
    assert summary['attention_count'] == 1, summary
    assert summary['overall_code'] == 'partial', summary
    assert quality.RATING_CONFIG_NOTE.startswith('评级采用项目自定义'), quality.RATING_CONFIG_NOTE
    print('五级评级 / 数据不足 / 摘要统计 OK')


def test_report():
    mt = quality.parse_metrics(PING_RAW, UP_RAW, DOWN_RAW)
    quality.evaluate(mt, {'bandwidth': '500M'})
    run = {
        'created_at': '2026-09-19 12:00:00', 'finished_at': '2026-09-19 12:10:00',
        'target_name': '目标VPS', 'target_host': '1.2.3.4',
        'target_region': '洛杉矶', 'target_bandwidth': '1G', 'error': '',
    }
    items = [
        {'machine_name': 'HK-01', 'machine_region': '香港', 'machine_bandwidth': '500M',
         'machine_host': '5.6.7.8', 'status': 'done', 'error': '',
         'metrics': __import__('json').dumps(mt, ensure_ascii=False)},
        {'machine_name': 'JP-02', 'machine_region': '东京', 'machine_bandwidth': '200M',
         'machine_host': '9.9.9.9', 'status': 'failed', 'error': 'SSH 连接失败', 'metrics': '',
         'up_raw': 'iperf3: error - unable to connect'},
    ]
    report = quality.build_report(run, {'name': '目标VPS', 'host': '1.2.3.4', 'region': '洛杉矶', 'bandwidth': '1G'}, items)
    assert ('| HK-01 | 香港 | 500M | 0% | 144.4 ms | 0.26 ms | 137 Mbit/s '
            '| 108 Mbit/s | 0 | **C 较差** | 上下行吞吐偏低 |') in report
    assert '| JP-02 | 东京 | 200M | — | — | — | — | — | — | **F 失败** | SSH 连接失败 |' in report
    assert '目标机器' not in report and '测试协议' not in report and '测试参数' not in report
    assert '## LinkPilot 测试结果' in report
    assert '### 原始数据' in report
    assert '#### 后端机器1：HK-01（香港 · 500M · 5.6.*.* · 连接端口 5201）' in report
    assert 'rtt min/avg/max/mdev = 144.239/144.402/146.898/0.255 ms' in report
    assert 'iperf3: error - unable to connect' in report
    assert '诊断详情' not in report
    print('---- 报告预览 ----')
    print(report)
    return report


def test_xff_and_limiter():
    """Only a proven proxy secret allows forwarding headers to affect client IP."""
    import app.app as app_mod
    from app import db
    from app.app import app as flask_app
    db.init_db()
    c = flask_app.test_client()
    db.clear_auth_failures('login:127.0.0.1')

    # 1) CF 直连（remote 属于 CF 网段）：CF-Connecting-IP 最可信
    m = db.create_machine({'name': 'xff', 'role': 'backend', 'region': '', 'bandwidth': ''})
    c.post('/api/agent/heartbeat',
           headers={'X-Agent-Token': m['token'],
                    'X-Agent-Protocol': '2',
                    'CF-Connecting-IP': '203.0.113.7',
                    'X-Forwarded-For': '1.2.3.4'},
           environ_base={'REMOTE_ADDR': '172.70.0.5'})
    assert db.get_machine(m['id'])['agent_ip'] == '203.0.113.7'

    # 2) 私网来源但没有代理密钥：客户端自带的转发头一律忽略
    m1b = db.create_machine({'name': 'xff-caddy', 'role': 'backend', 'region': '', 'bandwidth': ''})
    c.post('/api/agent/heartbeat',
           headers={'X-Agent-Token': m1b['token'],
                    'X-Agent-Protocol': '2',
                    'CF-Connecting-IP': '9.9.9.9',
                    'X-Forwarded-For': '203.0.113.7'},
           environ_base={'REMOTE_ADDR': '127.0.0.1'})
    assert db.get_machine(m1b['id'])['agent_ip'] == '127.0.0.1'

    # 2b) Caddy 注入正确代理密钥后，才接受它写入的 CF/XFF 头
    old_secret = app_mod.PROXY_SECRET
    app_mod.PROXY_SECRET = 'test-proxy-secret'
    m1c = db.create_machine({'name': 'xff-cfchain', 'role': 'backend', 'region': '', 'bandwidth': ''})
    c.post('/api/agent/heartbeat',
           headers={'X-Agent-Token': m1c['token'],
                    'X-Agent-Protocol': '2',
                    'X-LinkPilot-Proxy-Secret': 'test-proxy-secret',
                    'CF-Connecting-IP': '173.245.48.5',
                    'X-Forwarded-For': '203.0.113.77'},
           environ_base={'REMOTE_ADDR': '172.18.0.1'})
    assert db.get_machine(m1c['id'])['agent_ip'] == '173.245.48.5'

    # 2c) 错误代理密钥仍然不能污染地址
    m1d = db.create_machine({'name': 'xff-cfedge', 'role': 'target', 'region': '', 'bandwidth': ''})
    c.post('/api/agent/heartbeat',
           headers={'X-Agent-Token': m1d['token'],
                    'X-Agent-Protocol': '2',
                    'X-LinkPilot-Proxy-Secret': 'wrong',
                    'CF-Connecting-IP': '203.0.113.10',
                    'X-Forwarded-For': '172.67.131.196'},
           environ_base={'REMOTE_ADDR': '172.18.0.1'})
    assert db.get_machine(m1d['id'])['agent_ip'] == '172.18.0.1', \
        db.get_machine(m1d['id'])['agent_ip']
    app_mod.PROXY_SECRET = old_secret

    # 2d) 管理口仅绑回环时，允许本机 Nginx/SSH 隧道写入 XFF
    old_trust = app_mod.TRUST_LOCAL_PROXY
    app_mod.TRUST_LOCAL_PROXY = True
    m1e = db.create_machine({'name': 'xff-local-proxy', 'role': 'backend',
                             'region': '', 'bandwidth': ''})
    c.post('/api/agent/heartbeat',
           headers={'X-Agent-Token': m1e['token'],
                    'X-Agent-Protocol': '2',
                    'X-Forwarded-For': '203.0.113.88'},
           environ_base={'REMOTE_ADDR': '127.0.0.1'})
    assert db.get_machine(m1e['id'])['agent_ip'] == '203.0.113.88'
    app_mod.TRUST_LOCAL_PROXY = old_trust

    # 3) 公网直连伪造 XFF/CF 头 → 一律不采信，取 remote_addr
    m2 = db.create_machine({'name': 'xff2', 'role': 'backend', 'region': '', 'bandwidth': ''})
    c.post('/api/agent/heartbeat',
           headers={'X-Agent-Token': m2['token'], 'X-Agent-Protocol': '2',
                    'X-Forwarded-For': '6.6.6.6',
                    'CF-Connecting-IP': '7.7.7.7'},
           environ_base={'REMOTE_ADDR': '93.184.216.34'})
    assert db.get_machine(m2['id'])['agent_ip'] == '93.184.216.34'

    # 4) 限速桶用可信客户端 IP；锁定写入 SQLite，初始化后仍然存在
    for i in range(5):
        c.post('/api/login',
               headers={'X-Forwarded-For': f'10.0.0.{i}', 'CF-Connecting-IP': f'10.1.1.{i}'},
               environ_base={'REMOTE_ADDR': '127.0.0.1'},
               json={'user': 'x', 'password': 'y'})
    resp = c.post('/api/login',
                  headers={'X-Forwarded-For': '10.9.9.9', 'CF-Connecting-IP': '10.1.1.99'},
                  environ_base={'REMOTE_ADDR': '127.0.0.1'},
                  json={'user': 'x', 'password': 'y'})
    assert resp.status_code == 429, resp.get_data(as_text=True)
    db.init_db()
    assert db.auth_limited_seconds('login:127.0.0.1') > 0
    db.clear_auth_failures('login:127.0.0.1')
    print('可信代理密钥 / 伪造头不可信 / 持久化登录锁定 OK')


def test_test_protocol():
    """IPv4/IPv6 测试地址：心跳按协议族分别记录、探测结果只补空缺、报告标注协议。"""
    import json as _json
    from app import agent_jobs, db, runner

    # 1) 双栈面板下心跳来源会在 v4 / v6 之间跳动：两族地址必须都留下，不能互相覆盖
    m = db.create_machine({'name': 'dual', 'role': 'target', 'region': '', 'bandwidth': ''})
    db.touch_machine(m['id'], 'dual', '93.184.216.34')
    db.touch_machine(m['id'], 'dual', '2606:4700:4700::1111')
    got = db.get_machine(m['id'])
    assert got['ip4'] == '93.184.216.34', got
    assert got['ip6'] == '2606:4700:4700::1111', got

    # 2) 默认取 IPv4，只有显式要求时才取 IPv6
    assert db.machine_test_ip(got, 4) == '93.184.216.34'
    assert db.machine_test_ip(got, 6) == '2606:4700:4700::1111'

    # 3) 探测结果只补空缺（心跳观测到的地址更可信），force=True 才覆盖
    m2 = db.create_machine({'name': 'nat', 'role': 'target', 'region': '', 'bandwidth': ''})
    db.touch_machine(m2['id'], 'nat', '93.184.216.34')          # NAT 后的公网出口
    db.set_machine_ips(m2['id'], {'ip4': '10.0.0.5', 'ip6': '2606:4700:4700::1111'})
    got2 = db.get_machine(m2['id'])
    assert got2['ip4'] == '93.184.216.34', got2      # 私网探测值不覆盖观测值
    assert got2['ip6'] == '2606:4700:4700::1111', got2
    db.set_machine_ips(m2['id'], {'ip4': '93.184.216.99'}, force=True)
    assert db.get_machine(m2['id'])['ip4'] == '93.184.216.99'

    # 4) 探测输出解析：只收「协议族匹配 + 全局可路由」的地址
    found = agent_jobs.parse_ip_report(
        'IPV4=93.184.216.34\nIPV6=2606:4700:4700::1111\nIPV4=192.168.1.5\nIPV6=fd00::1\n')
    assert found == {'ip4': '93.184.216.34', 'ip6': '2606:4700:4700::1111'}, found
    assert agent_jobs.parse_ip_report('IPV4=2606:4700::1\nIPV6=93.184.216.34\n') == {}

    # 5) 目标机缺 IPv6 时，IPv6 测试必须直接拒绝（而不是跑出一堆失败）
    backend = db.create_machine({'name': 'be', 'role': 'backend', 'region': '', 'bandwidth': ''})
    db.touch_machine(backend['id'], 'be', '93.184.216.34')
    only4 = db.create_machine({'name': 'only4', 'role': 'target', 'region': '', 'bandwidth': ''})
    db.touch_machine(only4['id'], 'only4', '93.184.216.34')
    try:
        runner.start_run(only4['id'], [backend['id']], 6)
        raise AssertionError('缺 IPv6 地址时不应允许 IPv6 测试')
    except RuntimeError as e:
        assert 'IPv6' in str(e), e
    assert runner.active_run_id() is None

    # 6) 汇总报告不包含目标机地址、协议或参数，但仍保留原始数据段
    run6 = {'created_at': 't0', 'finished_at': 't1', 'ip_version': 6, 'error': '',
            'target_name': 'only6', 'target_host': '2606:4700:4700::1111'}
    rep = quality.build_report(run6, {'name': 'only6', 'host': '2606:4700:4700::1111',
                                      'region': '洛杉矶', 'bandwidth': '1G'}, [])
    assert '## LinkPilot 测试结果' in rep and '测试协议' not in rep, rep
    assert '### 原始数据' in rep, rep
    assert '2606:4700' not in rep and '1111' not in rep, rep
    run4 = dict(run6, ip_version=4, target_host='93.184.216.34')
    rep4 = quality.build_report(run4, {'name': 'v4', 'host': '93.184.216.34',
                                        'region': '', 'bandwidth': ''}, [])
    assert '测试协议' not in rep4 and '93.184' not in rep4, rep4
    print('地址探测 / 简洁报告 OK')


def test_discovery_flow():
    """端到端：面板派发地址探测任务 → Agent 回报 → 地址入库（走真实 Agent API 与签名任务）。"""
    import threading
    import time
    from app import db, runner
    from app.app import app as flask_app
    db.init_db()
    c = flask_app.test_client()

    m = db.create_machine({'name': 'disc', 'role': 'target', 'region': '', 'bandwidth': ''})
    db.touch_machine(m['id'], 'disc', '93.184.216.34')      # 只有 IPv4（心跳观测）

    result = {}
    t = threading.Thread(target=lambda: result.update(runner.discover_machine(m['id']) or {}),
                         daemon=True)
    t.start()
    # 面板把探测任务排进队列后，模拟 Agent 领取并回传本机地址
    job = None
    for _ in range(100):
        job = db.get_db().execute(
            "SELECT * FROM jobs WHERE machine_id=? AND status='queued' ORDER BY id DESC",
            (m['id'],)).fetchone()
        if job:
            break
        time.sleep(0.05)
    assert job, '面板未派发地址探测任务'
    body = 'IPV4=93.184.216.34\nIPV6=2606:4700:4700::1111\n'
    assert job['kind'] == 'report_ips', dict(job)
    assert db.next_queued_job(m['id'])['id'] == job['id']
    c.post('/api/agent/output',
           headers={'X-Agent-Token': m['token']},
           data={'job_id': job['id'], 'text': body, 'done': '1', 'exit_code': '0'})
    t.join(timeout=10)
    got = db.get_machine(m['id'])
    assert got['ip6'] == '2606:4700:4700::1111', got
    assert got['ip4'] == '93.184.216.34', got
    print('地址探测端到端 OK:', result)


def test_probe_script():
    """Agent report_ips：稳定 v6 优先、临时地址兜底、ULA/链路本地忽略。"""
    if os.name != 'posix':
        print('跳过 Agent report_ips 测试（仅 POSIX 环境执行）')
        return
    import shutil
    import subprocess
    from app import agent_jobs as aj

    if not shutil.which('bash') or not shutil.which('awk'):
        print('跳过 Agent report_ips 测试（缺 bash/awk）')
        return

    tmp = tempfile.mkdtemp(prefix='linkpilot-probe-')
    script = os.path.join(tmp, 'probe.sh')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'app', 'agent', 'install.sh'), encoding='utf-8') as f:
        installer = f.read()
    start = installer.index('report_ips() {')
    end = installer.index('\n}\n\nrun_job()', start) + 2
    with open(script, 'w', encoding='utf-8') as f:
        f.write('#!/bin/bash\n' + installer[start:end] + '\nreport_ips\n')

    # 假 ip / hostname：数据文件放在 stub 同目录，脚本内用 $(dirname "$0") 定位，
    # 不把绝对路径写进 stub（Windows 反斜杠路径在 MSYS 下不可靠）
    bins = {}
    for name, with_ip in (('bin', True), ('bin_noip', False)):
        d = os.path.join(tmp, name)
        os.makedirs(d)
        p = os.path.join(d, 'hostname')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\n[ "$1" = "-I" ] && cat "$(dirname "$0")/hn.txt"\n')
        os.chmod(p, 0o755)
        if with_ip:
            p = os.path.join(d, 'ip')
            with open(p, 'w', encoding='utf-8') as f:
                f.write('#!/bin/sh\n'
                        'd=$(dirname "$0")\n'
                        'case "$*" in\n'
                        '  "-4 route get 1.1.1.1") cat "$d/ip4.txt" ;;\n'
                        '  *"addr show"*) cat "$d/ip6.txt" ;;\n'
                        'esac\n')
            os.chmod(p, 0o755)
        else:
            # 显式放一个失败的 ip 占位：宿主若装了 iproute2，PATH 末尾仍能找到
            # 真命令，测试就会误走「有 ip」分支，无法覆盖 hostname -I 回退。
            p = os.path.join(d, 'ip')
            with open(p, 'w', encoding='utf-8') as f:
                f.write('#!/bin/sh\nexit 1\n')
            os.chmod(p, 0o755)
        bins[name] = d

    V4 = '1.1.1.1 via 10.0.0.1 dev eth0 src 93.184.216.34 uid 0\n'
    STABLE_TMP = ('2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n'
                  '    inet6 2408:8207:1234:5678::1/64 scope global\n'
                  '       valid_lft forever preferred_lft forever\n'
                  '    inet6 2408:8207:1234:5678:abcd:ef01:2345:6789/64 scope global temporary dynamic\n')
    TMP_ONLY = ('2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n'
                '    inet6 240e:3b0:1234:5678:9abc:def0:1234:5678/64 scope global temporary dynamic\n')
    ULA_LINK = ('2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n'
                '    inet6 fd00::1/64 scope global\n'
                '    inet6 fe80::216:3eff:fe12:3456/64 scope link\n')
    HN_MIX = '10.0.0.8 240e:3b0:1234:5678:9abc:def0:1234:5678 fe80::1'

    cases = [
        ('稳定地址优先于临时地址', 'bin', V4, STABLE_TMP, '',
         '93.184.216.34', '2408:8207:1234:5678::1'),
        ('只有临时地址时退用临时地址', 'bin', V4, TMP_ONLY, '',
         '93.184.216.34', '240e:3b0:1234:5678:9abc:def0:1234:5678'),
        ('腾讯云 /128 scope global dynamic', 'bin', V4,
         '2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n'
         '    inet6 2402:4e00:1020:1404:0:9a3f:1b2c:3d4e/128 scope global dynamic\n',
         '', '93.184.216.34', '2402:4e00:1020:1404:0:9a3f:1b2c:3d4e'),
        ('ULA / 链路本地必须忽略', 'bin', V4, ULA_LINK, '', '93.184.216.34', ''),
        ('无 ip 命令时回退 hostname -I', 'bin_noip', '', '', HN_MIX,
         '10.0.0.8', '240e:3b0:1234:5678:9abc:def0:1234:5678'),
    ]
    for title, binname, v4_out, v6_out, hn_out, want4, want6 in cases:
        for fn, text in (('ip4.txt', v4_out), ('ip6.txt', v6_out), ('hn.txt', hn_out)):
            with open(os.path.join(bins[binname], fn), 'w', encoding='utf-8') as f:
                f.write(text)
        env = dict(os.environ)
        env['PATH'] = bins[binname] + os.pathsep + os.environ.get('PATH', '')
        if binname == 'bin_noip':
            # 该用例依赖 stub 版的 hostname：个别环境（如 MSYS）PATH 会被重排导致
            # 系统 hostname 抢先，这时跳过而不是误报失败
            got_hn = subprocess.run(['bash', '-c', 'hostname -I'], capture_output=True,
                                    text=True, env=env).stdout.strip()
            if got_hn != hn_out.strip():
                print('跳过「无 ip 命令」用例：本环境的 hostname 无法被 stub 覆盖')
                continue
        out = subprocess.run(['bash', script], capture_output=True, text=True, env=env).stdout
        vals = dict(ln.split('=', 1) for ln in out.splitlines() if '=' in ln)
        assert vals.get('IPV4') == want4, (title, out)
        assert vals.get('IPV6') == want6, (title, out)

    # 探测结果能被面板解析（临时地址同样有效）
    got = aj.parse_ip_report('IPV4=93.184.216.34\nIPV6=240e:3b0:1234:5678:9abc:def0:1234:5678\n')
    assert got == {'ip4': '93.184.216.34',
                   'ip6': '240e:3b0:1234:5678:9abc:def0:1234:5678'}, got
    print('Agent report_ips 地址提取 OK（稳定/临时/ULA/无 ip 四种情况）')


def test_job_ids():
    """任务编号必须跨「面板数据库重置」继续递增。

    Agent 端防重放是「编号比本地水位小就拒收」，而 AUTOINCREMENT 在面板重置后会
    从 1 重新开始 —— 那会让整队 Agent 静默拒收所有任务（机器在线、却探测不到地址、
    测试全部超时）。所以编号必须用时间戳打底。
    """
    import time as _time
    from app import db
    db.init_db()
    m = db.create_machine({'name': 'jid', 'role': 'backend', 'region': '', 'bandwidth': ''})
    a = db.create_job(m['id'], 'report_ips', {}, 10)
    b = db.create_job(m['id'], 'report_ips', {}, 10)
    assert a > 10 ** 12, a                    # 时间戳打底，不再是 1、2、3
    assert b > a, (a, b)
    _time.sleep(0.005)                        # 模拟「面板数据库被重置」：清空任务表
    db.get_db().execute('DELETE FROM jobs')
    db.get_db().commit()
    c = db.create_job(m['id'], 'report_ips', {}, 10)
    assert c > b, (b, c)
    print('任务编号跨重置递增 OK:', a, b, c)


def test_probe_error_surfaced():
    """探测结果与失败原因都要落到机器资料上，面板才能说明「为什么没有 IPv6」。"""
    import threading
    import time as _time
    from app import db, runner
    from app.app import app as flask_app
    db.init_db()
    c = flask_app.test_client()
    m = db.create_machine({'name': 'probe', 'role': 'target', 'region': '', 'bandwidth': ''})
    db.touch_machine(m['id'], 'probe', '93.184.216.34')

    def probe_round(body):
        res = {}
        t = threading.Thread(
            target=lambda: res.update(runner.discover_machine(m['id']) or {}), daemon=True)
        t.start()
        job = None
        for _ in range(100):
            job = db.get_db().execute(
                "SELECT * FROM jobs WHERE machine_id=? AND status='queued' ORDER BY id DESC",
                (m['id'],)).fetchone()
            if job:
                break
            _time.sleep(0.05)
        assert job, '面板未派发地址探测任务'
        assert db.next_queued_job(m['id'])['id'] == job['id']
        c.post('/api/agent/output',
               headers={'X-Agent-Token': m['token']},
               data={'job_id': job['id'], 'text': body, 'done': '1', 'exit_code': '0'})
        t.join(timeout=10)
        return db.get_machine(m['id'])

    # 1) 探到全局地址：写入资料并清空失败原因
    got = probe_round('IPV4=93.184.216.34\nIPV6=240e:3b0:1234:5678::9\n')
    assert got['ip6'] == '240e:3b0:1234:5678::9', got
    assert got['probe_error'] == '', got

    # 2) 只有私网 / ULA（NAT 机典型情况）：地址不写，原因留在机器上供面板显示，
    #    且文案要说清这是正常现象而不是故障
    got = probe_round('IPV4=10.0.0.8\nIPV6=fd00::1\n')
    assert 'NAT' in got['probe_error'] and '全局' in got['probe_error'], got
    # NAT 机不该被每 10 分钟反复探测一次
    assert runner.DISCOVER_COOLDOWN_EMPTY > runner.DISCOVER_COOLDOWN
    # 有可用地址时（NAT 机用心跳观测到的出口 IP），资料里仍是可测地址
    db.touch_machine(m['id'], 'probe', '203.0.113.10')
    assert db.machine_test_ip(db.get_machine(m['id']), 4) == '203.0.113.10'

    # 3) 任务没回传（Agent 拒收/离线）时，提示要能指路
    assert 'agent.log' in runner._probe_fail_reason(TimeoutError('[x] 任务执行超时(20s): r4='))
    print('地址探测失败原因回传 OK:', got['probe_error'][:30])


def test_test_params():
    """测试参数：校验、iperf3 命令拼装、端口/协议落到 Agent 脚本上。"""
    from app import agent_jobs as aj
    from app import job_types
    from app import runner

    # 1) 默认值与历史行为一致（单线程 / 10 秒 / 5201 / ping 200 / TCP）
    cfg = runner.normalize_params({})
    assert cfg == {'streams': 1, 'duration': 10, 'port': 5201,
                   'ping_count': 200, 'udp': False, 'udp_bandwidth': '100M', 'ports': {}}, cfg

    # 2) 自定义值（字符串数字、带宽大小写归一）
    cfg = runner.normalize_params({'streams': '4', 'duration': 30, 'port': '6500',
                                   'ping_count': 50, 'udp': True, 'udp_bandwidth': '200m'})
    assert cfg == {'streams': 4, 'duration': 30, 'port': 6500, 'ping_count': 50,
                   'udp': True, 'udp_bandwidth': '200M', 'ports': {}}, cfg

    # 3) 越界/非法值必须报错，而不是悄悄换成别的参数
    for bad in ({'streams': 0}, {'streams': 99}, {'duration': 0}, {'duration': 9999},
                {'port': 0}, {'port': 70000}, {'ping_count': 0}, {'ping_count': 99999},
                {'streams': 'x'}, {'udp_bandwidth': '100'}):
        try:
            runner.normalize_params(bad)
            raise AssertionError(f'{bad} 应该被拒绝')
        except RuntimeError:
            pass

    # 4) 命令拼装：端口 / 线程 / 时长 / UDP / 下行
    tcp = dict(cfg, udp=False, udp_bandwidth='100M')
    assert runner.build_iperf_cmd('1.2.3.4', tcp) == 'iperf3 -c 1.2.3.4 -p 6500 -t 30 -P 4', tcp
    assert runner.build_iperf_cmd('1.2.3.4', tcp, reverse=True) == \
        'iperf3 -c 1.2.3.4 -p 6500 -t 30 -P 4 -R'
    udp_cmd = runner.build_iperf_cmd('2606:4700:4700::1111', dict(tcp, udp=True, udp_bandwidth='200M'))
    assert udp_cmd == 'iperf3 -c 2606:4700:4700::1111 -p 6500 -t 30 -P 4 -u -b 200M', udp_cmd

    # 5) 端口等参数只通过白名单结构化载荷下发
    srv = job_types.encode('start_server', {'port': 6500, 'ttl': 600, 'bind': '1.2.3.4'})
    assert 'port=6500' in srv and 'bind=1.2.3.4' in srv and 'kind=start_server' in srv, srv
    stop = job_types.encode('stop_server', {'ports': [6500]})
    assert 'ports=6500' in stop and 'kind=stop_server' in stop, stop
    check = job_types.encode('check_port', {'host': '1.2.3.4', 'port': 6500})
    assert 'host=1.2.3.4' in check and 'port=6500' in check, check
    assert aj.valid_port(0) == 5201 and aj.valid_port(70000) == 5201
    assert aj.valid_port('abc') == 5201 and aj.valid_port('6500') == 6500

    # 6) API 层：非法参数返回 400 且说明原因（合法参数会真的开跑，不在此测试）
    from app import db
    from app.app import app as flask_app
    db.init_db()
    t = db.create_machine({'name': 'param-t', 'role': 'target', 'region': '', 'bandwidth': ''})
    b = db.create_machine({'name': 'param-b', 'role': 'backend', 'region': '', 'bandwidth': ''})
    c = flask_app.test_client()
    _login(c)
    r = c.post('/api/runs', json={'target_id': t['id'], 'backend_ids': [b['id']], 'streams': 999})
    assert r.status_code == 400 and '线程数' in r.get_json()['error'], r.get_json()
    r = c.post('/api/runs', json={'target_id': t['id'], 'backend_ids': [b['id']],
                                  'udp': True, 'udp_bandwidth': '100'})
    assert r.status_code == 400 and '带宽' in r.get_json()['error'], r.get_json()

    # 7) 每台后端机可以单独指定端口：逐台落库 + 命令用各自的端口
    b2 = db.create_machine({'name': 'param-b2', 'role': 'backend', 'region': '', 'bandwidth': ''})
    run_id = db.create_run(t, [b['id'], b2['id']], 4, target_host='93.184.216.34',
                           port=5201, ports={b['id']: 6001},
                           streams=2, duration=5, udp=False, udp_bandwidth='100M', ping_count=10)
    items = db.get_run_items(run_id)
    assert [it['port'] for it in items] == [6001, 0], items      # 0 = 跟随目标机端口
    rcfg = runner.run_cfg(db.get_run(run_id))
    assert [runner.item_port(it, rcfg) for it in items] == [6001, 5201]
    assert runner.build_iperf_cmd('93.184.216.34', runner.item_cfg(rcfg, items[0])) == \
        'iperf3 -c 93.184.216.34 -p 6001 -t 5 -P 2'
    assert runner.build_iperf_cmd('93.184.216.34', runner.item_cfg(rcfg, items[1])) == \
        'iperf3 -c 93.184.216.34 -p 5201 -t 5 -P 2'
    assert runner.normalize_params({'ports': {'3': '6001'}})['ports'] == {3: 6001}
    for bad in ({'ports': {'1': 0}}, {'ports': {'1': 99999}}, {'ports': {'1': 'x'}},
                {'ports': {'x': 5201}}):
        try:
            runner.normalize_params(bad)
            raise AssertionError(f'{bad} 应该被拒绝')
        except RuntimeError:
            pass

    # 8) 换端口（含 NAT 机：商家端口映射一般是同号映射，如公网 43343 → 内网 43343）
    #    只改「目标机端口」，没单独改过的后端机必须跟着连新端口（曾经这里会连旧端口 5201）
    cfg_nat = runner.normalize_params({'port': '43343'})
    assert cfg_nat['port'] == 43343
    follow = [{'machine_name': 'A', 'port': 0}, {'machine_name': 'B', 'port': 0}]
    assert runner.target_port_of(cfg_nat) == 43343
    assert runner.serve_ports(follow, cfg_nat) == [43343]
    assert [runner.item_port(it, cfg_nat) for it in follow] == [43343, 43343]
    assert runner.item_cfg(cfg_nat, follow[0])['port'] == 43343
    assert runner.build_iperf_cmd('1.2.3.4', runner.item_cfg(cfg_nat, follow[0])) == \
        'iperf3 -c 1.2.3.4 -p 43343 -t 10 -P 1'
    # 多机各用各端口（目标机有公网 IP）：目标机端口 + 各机单独指定的连接端口都要起
    cfg2 = runner.normalize_params({'port': '5201'})
    items2 = [{'machine_name': 'A', 'port': 6001}, {'machine_name': 'B', 'port': 0},
              {'machine_name': 'C', 'port': 6002}]
    assert runner.serve_ports(items2, cfg2) == [5201, 6001, 6002]
    assert [runner.item_port(it, cfg2) for it in items2] == [6001, 5201, 6002]
    # 越界端口仍要报错
    try:
        runner.normalize_params({'port': 70000})
        raise AssertionError('越界端口应被拒绝')
    except RuntimeError:
        pass
    print('测试参数校验 / iperf3 命令拼装 OK:', runner.build_iperf_cmd('1.2.3.4', tcp))


def _login(client):
    """测试用：读面板首次初始化写在 .initial_password 的密码登录一次。

    先清掉登录限速状态——前面的用例会故意打失败登录把测试客户端 IP 锁住。
    """
    from app import db
    db.get_db().execute('DELETE FROM auth_limits')
    db.get_db().commit()
    user, _ = db.get_auth()
    with open(db.INITIAL_PW_PATH, encoding='utf-8') as f:
        pw = f.read().strip()
    r = client.post('/api/login', json={'user': user, 'password': pw})
    assert r.status_code == 200, r.get_json()


def test_multistream_and_udp_parse():
    """多线程（-P）取 [SUM] 汇总行；UDP 的丢包/抖动取接收端，且不适用重传。"""
    from app import quality

    up = ('[ ID] Interval           Transfer     Bitrate         Retr\n'
          '[  4]   0.00-10.00  sec  25.0 MBytes  21.0 Mbits/sec    0             sender\n'
          '[  4]   0.00-10.00  sec  24.9 MBytes  20.9 Mbits/sec                  receiver\n'
          '[  6]   0.00-10.00  sec  25.1 MBytes  21.0 Mbits/sec    1             sender\n'
          '[  6]   0.00-10.00  sec  25.0 MBytes  20.9 Mbits/sec                  receiver\n'
          '[SUM]   0.00-10.00  sec   100 MBytes  83.9 Mbits/sec    1             sender\n'
          '[SUM]   0.00-10.00  sec  99.8 MBytes  83.7 Mbits/sec                  receiver\n')
    s, r = quality.parse_iperf(up)
    assert s['mbits'] == 83.9 and r['mbits'] == 83.7 and s['retr'] == 1, (s, r)
    assert 'SUM' in quality.summary_block(up)

    udp_up = ('[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams\n'
              '[  4]   0.00-10.00  sec   115 MBytes  96.4 Mbits/sec  0.000 ms  0/8320 (0%)  sender\n'
              '[  4]   0.00-10.00  sec   114 MBytes  95.9 Mbits/sec  0.019 ms  12/8320 (0.14%)  receiver\n')
    udp_down = ('[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams\n'
                '[  4]   0.00-10.00  sec   110 MBytes  92.3 Mbits/sec  0.000 ms  0/7960 (0%)  sender\n'
                '[  4]   0.00-10.00  sec   109 MBytes  91.6 Mbits/sec  0.031 ms  40/7960 (0.5%)  receiver\n')
    mt = quality.parse_metrics(PING_RAW, udp_up, udp_down)
    assert mt['udp'] is True
    assert mt['up_udp_loss_pct'] == 0.14 and mt['down_udp_loss_pct'] == 0.5, mt
    assert mt['up_jitter_ms'] == 0.019 and mt['down_jitter_ms'] == 0.031, mt
    assert mt['retr_total'] is None, mt          # UDP 没有重传概念
    assert mt['up_mbits'] == 96.4 and mt['down_mbits'] == 91.6, mt
    quality.evaluate(mt, {'bandwidth': ''}, {'duration': 10})
    assert '丢包 0.5%' in mt['rating_detail'] and '不参与评级' in mt['rating_detail'], mt
    assert mt['rating_code'] in ('S', 'A', 'B', 'C'), mt
    print('多线程 / UDP 解析 OK:', mt['rating'], '|', mt['rating_detail'])


def test_report_params_and_udp_columns():
    """简洁报告不重复参数；UDP 测试使用 UDP 丢包列。"""
    import json as _json
    from app import quality

    udp_up = ('[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams\n'
              '[  4]   0.00-10.00  sec   115 MBytes  96.4 Mbits/sec  0.000 ms  0/8320 (0%)  sender\n'
              '[  4]   0.00-10.00  sec   114 MBytes  95.9 Mbits/sec  0.019 ms  12/8320 (0.14%)  receiver\n')
    mt = quality.parse_metrics(PING_RAW, udp_up, udp_up)
    quality.evaluate(mt, {'bandwidth': ''})
    items = [{'machine_name': 'HK-01', 'machine_region': '香港', 'machine_bandwidth': '500M',
              'machine_host': '5.6.7.8', 'status': 'done', 'error': '', 'port': 6001,
              'metrics': _json.dumps(mt, ensure_ascii=False)},
             {'machine_name': 'JP-02', 'machine_region': '东京', 'machine_bandwidth': '200M',
              'machine_host': '9.9.9.9', 'status': 'done', 'error': '', 'port': 5201,
              'metrics': _json.dumps(mt, ensure_ascii=False)}]
    run = {'created_at': 't0', 'finished_at': 't1', 'ip_version': 4, 'error': '',
           'target_name': 'T', 'target_host': '1.2.3.4', 'streams': 4, 'duration': 30,
           'port': 5201, 'udp': 1, 'udp_bandwidth': '200M', 'ping_count': 50}
    rep = quality.build_report(run, {'name': 'T', 'host': '1.2.3.4', 'region': '', 'bandwidth': ''}, items)
    assert '| UDP丢包 |' in rep, rep
    assert '| 重传 |' not in rep, rep
    assert '测试参数' not in rep and '### 原始数据' in rep, rep
    assert '各机连接端口' not in rep, rep

    # 只有一台机器、端口一致时不出现「各机连接端口」这一行
    one = [dict(items[0], port=5201)]
    rep3 = quality.build_report(run, {'name': 'T', 'host': '1.2.3.4', 'region': '', 'bandwidth': ''}, one)
    assert '各机连接端口' not in rep3, rep3

    # 换端口（NAT 同号映射）：报告里的目标机端口与后端连接端口都是新端口
    nat_run = dict(run, port=43343, udp=0, streams=1, duration=10,
                   udp_bandwidth='100M', ping_count=200)
    rep4 = quality.build_report(nat_run, {'name': 'T', 'host': '1.2.3.4', 'region': '', 'bandwidth': ''},
                                [dict(items[0], port=0)])
    assert '测试参数' not in rep4, rep4
    assert '#### 后端机器1：HK-01（香港 · 500M · 5.6.*.* · 连接端口 43343）' in rep4, rep4

    # 旧记录（没有参数列）退回默认参数，TCP 表头保留重传列
    old = {'created_at': 't0', 'finished_at': 't1', 'ip_version': 4, 'error': '',
           'target_name': 'T', 'target_host': '1.2.3.4'}
    rep2 = quality.build_report(old, {'name': 'T', 'host': '1.2.3.4', 'region': '', 'bandwidth': ''}, items)
    assert '| 重传 |' in rep2, rep2
    print('简洁报告 / UDP 列 OK')


def test_machine_addr_override():
    """手动测试地址：NAT / 反代后面板观测到的地址不对时的兜底（填了就按它测）。"""
    from app import db
    from app.app import app as flask_app
    db.init_db()
    c = flask_app.test_client()
    _login(c)

    # 建机器时带手动地址（用 RFC 5737 文档地址，别把真实 IP 写进测试数据）
    r = c.post('/api/machines', json={'name': 'nat-t', 'role': 'target', 'region': '德国',
                                      'bandwidth': '1G', 'addr_override': '203.0.113.10'})
    assert r.status_code == 200, r.get_json()
    m = r.get_json()
    assert m['addr_override'] == '203.0.113.10'
    # 心跳观测到的地址（哪怕是错的 CDN 地址）不能覆盖手动指定
    db.touch_machine(m['id'], 'nat-t', '172.67.131.196')
    m = db.get_machine(m['id'])
    assert m['ip4'] == '172.67.131.196'
    assert db.machine_test_ip(m, 4) == '203.0.113.10'
    # 手动地址是 v6 时只影响 v6，v4 仍用自动探测值
    db.update_machine(m['id'], {'name': 'nat-t', 'role': 'target', 'region': '德国',
                                'bandwidth': '1G', 'addr_override': '2606:4700:4700::1111'})
    m = db.get_machine(m['id'])
    assert db.machine_test_ip(m, 6) == '2606:4700:4700::1111'
    assert db.machine_test_ip(m, 4) == '172.67.131.196'
    # 清空后回归自动
    db.update_machine(m['id'], {'name': 'nat-t', 'role': 'target', 'region': '德国',
                                'bandwidth': '1G', 'addr_override': ''})
    assert db.machine_test_ip(db.get_machine(m['id']), 4) == '172.67.131.196'
    # 非法地址要被拒绝（400），不能悄悄存进去
    r = c.post('/api/machines', json={'name': 'bad', 'role': 'backend', 'addr_override': '1.2.3'})
    assert r.status_code == 400 and 'IP' in r.get_json()['error'], r.get_json()
    print('手动测试地址 OK')


def test_machine_order_persisted():
    """机器列表顺序由面板拖拽保存，刷新和多端看到同一顺序。"""
    from app import db
    from app.app import app as flask_app
    db.init_db()
    c = flask_app.test_client()
    _login(c)

    before = db.get_machines()
    while len(before) < 3:
        db.create_machine({
            'name': f'order-{len(before)}', 'role': 'backend',
            'region': '', 'bandwidth': '', 'addr_override': '',
        })
        before = db.get_machines()
    wanted = [before[-1]['id']] + [m['id'] for m in before[:-1]]
    response = c.put('/api/machines/order', json={'ids': wanted})
    assert response.status_code == 200, response.get_json()
    assert [m['id'] for m in response.get_json()['machines']] == wanted
    assert [m['id'] for m in db.get_machines()] == wanted

    bad = c.put('/api/machines/order', json={'ids': []})
    assert bad.status_code == 400
    print('机器拖拽顺序持久化 OK')


def test_server_lifecycle():
    """iperf3 server 生命周期：存活上限按本轮时长收敛 + 任何退出路径都关闭。

    背景：iperf3 是裸 -s（无认证、无来源限制），端口只要还在监听就可能被扫描到并
    盗刷流量，所以「什么时候关」必须被测试固定住。
    """
    import inspect
    import shutil
    import subprocess
    import time
    from app import agent_jobs as aj
    from app import runner

    # 1) TTL 收敛：正常值原样、越界夹紧、非法值退回默认
    assert aj.clamp_ttl(600) == 600
    assert aj.clamp_ttl(1) == aj.SERVER_TTL_MIN == 300
    assert aj.clamp_ttl(10 ** 9) == aj.SERVER_TTL_MAX == 3600
    assert aj.clamp_ttl('abc') == aj.SERVER_TTL_DEFAULT == 1800
    assert aj.clamp_ttl('900') == 900 and aj.clamp_ttl(900.7) == 900

    # 2) 结构化任务自身限制 TTL / 端口，且 Agent 本地模板包含绑定与 TTL
    from app import job_types
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'app', 'agent', 'install.sh'), encoding='utf-8') as f:
        installer = f.read()
    assert 'iperf3 -s -B "$chosen" -p "$port"' in installer
    assert 'timeout "$ttl"' in installer
    for bad in (
        {'port': 0, 'ttl': 600, 'bind': '1.2.3.4'},
        {'port': 5201, 'ttl': 1, 'bind': '1.2.3.4'},
    ):
        try:
            job_types.normalize('start_server', bad)
            raise AssertionError(f'应拒绝非法 server 参数：{bad}')
        except ValueError:
            pass

    # 4) 预估 TTL = iperf3 串行关键路径 + 余量：短测试不再白留 30 分钟，
    #    而 3 台 × 300 秒这种长测（旧代码固定 1800 会在中途过期打断测试）要够用
    cfg10 = runner.normalize_params({'duration': 10})
    one = runner.server_ttl([{'id': 1}], cfg10)
    assert one == 2 * 10 + 10 + 120 + 300 == 450, one
    t2 = runner.server_ttl([{'id': 1}, {'id': 2}], cfg10)
    assert one < t2 <= aj.SERVER_TTL_MAX, (one, t2)
    long3 = runner.server_ttl([{'id': i} for i in range(1, 4)],
                              runner.normalize_params({'duration': 300}))
    assert long3 == 3 * 610 + 120 + 300 == 2250, long3           # 不再中途过期
    many = runner.server_ttl([{'id': i} for i in range(1, 33)],
                             runner.normalize_params({'duration': 300}))
    assert many == aj.SERVER_TTL_MAX == 3600, many               # 超长测封顶，靠预检自愈续期

    # 5) 回归护栏：关闭动作必须在 _worker 的 finally 里（正常结束 / 手动停止 / 异常
    #    都要收尾），而不是只在成功路径上关一次
    src = inspect.getsource(runner._worker)
    assert len(src.split('finally:')) == 2, src
    assert "'stop_server'" in src.split('finally:', 1)[1], src
    assert 'started_ports' in src, src

    # 6) 真执行（仅 Linux + bash）：直接源码执行 Agent 内嵌的白名单处理函数
    if sys.platform != 'linux' or not shutil.which('bash') or not shutil.which('timeout'):
        print('跳过 server 脚本真执行用例（仅 Linux + bash/coreutils 环境执行）')
        print('iperf3 server 生命周期（TTL / 收尾关闭）OK')
        return

    start = installer.index('P() {')
    end = installer.index('\nwhile true; do', start)
    functions = installer[start:end]
    tmp = tempfile.mkdtemp(prefix='linkpilot-srv-')
    bind = os.path.join(tmp, 'bin')
    os.makedirs(bind)
    with open(os.path.join(bind, 'iperf3'), 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\nexec sleep 300\n')          # 假 iperf3：exec 后 PID 就是它
    os.chmod(os.path.join(bind, 'iperf3'), 0o755)
    env = dict(os.environ, PATH=bind + os.pathsep + os.environ.get('PATH', ''))

    def alive(pid):
        """进程是否真的活着（僵尸不算：容器里 PID 1 可能不回收子进程）。"""
        try:
            with open(f'/proc/{int(pid)}/stat', encoding='utf-8') as fp:
                return fp.read().rsplit(')', 1)[1].split()[0] != 'Z'
        except (OSError, ValueError, IndexError):
            return False

    spool = tempfile.mkdtemp(prefix='linkpilot-agent-job-')
    script_path = os.path.join(spool, 'run-job.sh')
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write('#!/bin/bash\nset -eu\n')
        f.write(f'SPOOL={spool}\n')
        f.write(functions)
        f.write('\nrun_job\n')
    os.chmod(script_path, 0o700)

    def run_job(kind, params):
        with open(os.path.join(spool, 'payload'), 'w', encoding='utf-8') as f:
            f.write(job_types.encode(kind, params))
        return subprocess.run(['bash', script_path], capture_output=True, text=True, env=env)

    stop_port = 65011
    pidf = f'/tmp/iperf3-server-{stop_port}.pid'
    try:
        r = run_job('start_server', {'port': stop_port, 'ttl': 600, 'bind': '127.0.0.1'})
        assert f'SERVER_STARTED port={stop_port} ttl=600' in r.stdout, (r.stdout, r.stderr)
        pid = open(pidf, encoding='utf-8').read().strip()
        assert alive(pid), '假 iperf3 没起来'

        r = run_job('stop_server', {'ports': [stop_port]})
        assert f'IPERF3_SERVER_STOPPED ports={stop_port}' in r.stdout, r.stdout
        time.sleep(0.5)
        assert not alive(pid), '关闭脚本没有真的杀掉 server'
        assert not os.path.exists(pidf), '关闭后 pid 文件没清理'
    finally:
        for f in (pidf,):
            if os.path.exists(f):
                try:
                    os.kill(int(open(f, encoding='utf-8').read().strip()), 9)
                except (OSError, ValueError):
                    pass
                os.remove(f)
    print('iperf3 server 生命周期（TTL / 收尾关闭 / 脚本真执行）OK')


def test_daemon_guard():
    """发行版自带 iperf3 常驻服务的体检：探测脚本、解析、提示语、apt 不弹窗的保证。"""
    from app import runner

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'app', 'agent', 'install.sh'), encoding='utf-8') as f:
        installer = f.read()
    for needle in ('DISTRO_DAEMON=$state', 'PORT_BUSY=$p $line',
                   '/tmp/iperf3-server-$p.pid', 'ss -ltnp', 'netstat -ltnp'):
        assert needle in installer, needle

    # 2) 解析：几种状态 / 端口占用 / 任务没跑成（只提示，不误判为故障）
    state, busy = runner.parse_daemon_report(
        'DISTRO_DAEMON=active\n'
        'PORT_BUSY=5201 LISTEN 0 128 0.0.0.0:5201 0.0.0.0:* users:(("iperf3",pid=812,fd=3))\n'
        'DAEMON_CHECK_DONE\n')
    assert state == 'active' and 5201 in busy and 'iperf3' in busy[5201], (state, busy)
    assert runner.parse_daemon_report('DISTRO_DAEMON=enabled\n') == ('enabled', {})
    assert runner.parse_daemon_report('') == ('unknown', {})
    assert runner.parse_daemon_report(None) == ('unknown', {})

    # 3) 提示语要给出能直接照做的办法（面板用户不一定懂 shell）
    assert 'systemctl disable --now iperf3' in runner._DISTRO_DAEMON_HINT
    assert '目标机端口' in runner._DISTRO_DAEMON_HINT

    # Agent 安装时预装依赖；openSUSE 保留 iperf 包名回退
    assert 'ensure_pkg iperf3 iperf3 iperf3 iperf3' in installer
    assert 'zypper --non-interactive install iperf' in installer
    print('发行版 iperf3 守护进程体检 / apt 非交互安装 OK')


def test_job_cleanup_paths():
    """任务收尾路径：作废排队任务 / 标记过期测试不能抛异常（曾在此处踩过 NameError）。

    背景：_worker 的 finally 里会调用 cancel_queued_jobs，一旦它抛异常，
    整个测试线程会静默死掉——面板上表现为「测试永远停在运行中、报告不生成」。
    """
    from app import db
    db.init_db()
    t = db.create_machine({'name': 'clean-t', 'role': 'target', 'region': '', 'bandwidth': ''})
    b = db.create_machine({'name': 'clean-b', 'role': 'backend', 'region': '', 'bandwidth': ''})

    # 队列里三个任务：两个属于本次机器、一个是别人的
    j1 = db.create_job(b['id'], 'report_ips', {}, 30)
    j2 = db.create_job(b['id'], 'report_ips', {}, 30)
    other = db.create_machine({'name': 'clean-x', 'role': 'backend', 'region': '', 'bandwidth': ''})
    j3 = db.create_job(other['id'], 'report_ips', {}, 30)
    db.create_job(t['id'], 'report_ips', {}, 30)

    db.cancel_queued_jobs([b['id'], t['id']])              # 不能抛
    assert db.get_job(j1)['status'] == 'failed', db.get_job(j1)
    assert db.get_job(j2)['status'] == 'failed'
    assert db.get_job(j3)['status'] == 'queued', db.get_job(j3)   # 别的机器不受影响
    assert '任务已取消' in db.get_job(j1)['output'], db.get_job(j1)['output']
    db.cancel_queued_jobs([])                              # 空列表也要能过
    db.cancel_queued_jobs(None)

    # 重启自愈：running/pending 的测试标失败，排队任务作废
    rid = db.create_run(t, [b['id']], 4, target_host='93.184.216.34', port=5201, streams=1,
                        duration=1, udp=False, udp_bandwidth='100M', ping_count=1)
    db.mark_stale_runs()
    assert db.get_run(rid)['status'] == 'failed', db.get_run(rid)
    assert db.get_job(j3)['status'] == 'failed', db.get_job(j3)
    print('任务收尾路径（取消排队 / 重启自愈）OK')


def test_orphan_server_reclaim():
    """面板重启遗留的孤儿 iperf3 server：按记录端口精确回收（否则后一轮必然撞端口）。

    真实场景：面板在「server 已起、关闭指令未送出」的窗口里被重启，目标机上留下裸
    server 占着端口，下一轮（尤其是定时任务）启动 server 必然失败。
    """
    from app import db, runner
    db.init_db()
    t = db.create_machine({'name': 'orph-t', 'role': 'target', 'region': '', 'bandwidth': ''})
    b = db.create_machine({'name': 'orph-b', 'role': 'backend', 'region': '', 'bandwidth': ''})
    db.touch_machine(t['id'], 'orph-t', '93.184.216.34')
    db.touch_machine(b['id'], 'orph-b', '93.184.216.34')

    # 中断的测试：只起了 5201 就崩了
    rid = db.create_run(t, [b['id']], 4, target_host='93.184.216.34', port=5201, streams=1,
                        duration=1, udp=False, udp_bandwidth='100M', ping_count=1)
    db.update_run(rid, served_ports='6002 5201')       # 本轮起过的端口（有序落库）
    db.mark_stale_runs()                               # 面板重启自愈

    assert db.get_run(rid)['status'] == 'failed', db.get_run(rid)
    recs = db.runs_with_orphan_ports()
    assert any(r['id'] == rid for r in recs), recs

    calls = []
    real_job = runner._job

    def fake_job(machine, kind, params, timeout, log, stop_check):
        calls.append((machine['id'], kind, params))
        return 0, 'IPERF3_SERVER_STOPPED'

    runner._job = fake_job
    try:
        done = runner.clear_orphan_servers(force=True)
    finally:
        runner._job = real_job
    assert rid in done, (done, calls)
    assert calls and calls[0][0] == t['id'], calls
    assert calls[0][1] == 'stop_server' and sorted(calls[0][2]['ports']) == [5201, 6002], calls
    # 回收过就不再重复派发
    assert not db.runs_with_orphan_ports(), db.runs_with_orphan_ports()
    assert runner.clear_orphan_servers(force=True) == []

    # 机器离线时不派发（等它上线再回收），也不会把记录清掉
    rid2 = db.create_run(t, [b['id']], 4, target_host='93.184.216.34', port=5201, streams=1,
                         duration=1, udp=False, udp_bandwidth='100M', ping_count=1)
    db.update_run(rid2, served_ports='5201')
    db.mark_stale_runs()
    db.get_db().execute('UPDATE machines SET last_seen=0 WHERE id=?', (t['id'],))
    db.get_db().commit()
    assert runner.clear_orphan_servers(force=True) == []
    assert any(r['id'] == rid2 for r in db.runs_with_orphan_ports())
    print('面板重启遗留 server 回收 OK')


def test_schedule_params():
    """定时任务字段校验：间隔收敛、参数与手动测试同一套、非法值给出可读原因。"""
    from unittest.mock import patch
    from app import scheduler
    from app.app import app as flask_app
    from app import db
    db.init_db()

    # 1) 校验与默认值
    s = scheduler.normalize_schedule({'target_id': 7, 'backend_ids': [1, 2, 2]})
    assert s['target_id'] == 7 and s['backend_ids'] == [1, 2], s     # 去重
    assert s['interval_seconds'] == 3600 and s['on_busy'] == 'skip', s
    assert s['lifetime_seconds'] == 0, s                             # 旧语义：默认不限时
    assert s['name'] == '定时任务' and s['enabled'] == 1, s
    s = scheduler.normalize_schedule({'target_id': 7, 'backend_ids': ['1'], 'name': '  长期对比  ',
                                      'interval_seconds': '900', 'on_busy': 'wait', 'udp': True,
                                      'udp_bandwidth': '50m', 'ports': {'1': 6001, '2': 'x'}})
    assert s['name'] == '长期对比' and s['interval_seconds'] == 900 and s['on_busy'] == 'wait', s
    assert s['udp_bandwidth'] == '50M' and s['ports'] == {1: 6001}, s
    s = scheduler.normalize_schedule({'target_id': 7, 'backend_ids': [1],
                                      'lifetime_seconds': 7 * 86400})
    assert s['lifetime_seconds'] == 7 * 86400, s

    # 2) 非法值一律拒绝，不能悄悄换成别的参数
    for bad in ({'target_id': 0, 'backend_ids': [1]},
                {'target_id': 7},
                {'target_id': 7, 'backend_ids': []},
                {'target_id': 7, 'backend_ids': [1], 'interval_seconds': 10},        # 最短 5 分钟
                {'target_id': 7, 'backend_ids': [1], 'interval_seconds': 10 ** 9},   # 最长 30 天
                {'target_id': 7, 'backend_ids': [1], 'lifetime_seconds': -1},
                {'target_id': 7, 'backend_ids': [1], 'lifetime_seconds': 366 * 86400},
                {'target_id': 7, 'backend_ids': [1], 'streams': 0},
                {'target_id': 7, 'backend_ids': [1], 'duration': 9999}):
        try:
            scheduler.normalize_schedule(bad)
            raise AssertionError(f'{bad} 应该被拒绝')
        except scheduler.InvalidSchedule:
            pass

    # 3) 人话化的间隔与倒计时（面板直接显示这些字符串）；数字与单位之间是不换行空格
    assert scheduler._human_duration(300) == '5\u00a0分钟'
    assert scheduler._human_duration(3600) == '1\u00a0小时'
    assert scheduler._human_duration(86400) == '1\u00a0天'
    assert scheduler._human_duration(90000) == '25\u00a0小时'
    assert '\u00a0' in scheduler._human_duration(600), '间隔文案必须是单个不可断行的词'
    assert scheduler._human_delta(-5) == '即将执行'
    assert scheduler._human_delta(59) == '59 秒后'
    assert scheduler._human_delta(3599) == '59 分钟后'
    assert scheduler._human_delta(7200) == '2 小时 0 分后'

    # 4) 下次执行时间：固定间隔语义（现在 + 间隔），并夹住上下限
    now = 1_700_000_000.0
    assert scheduler.compute_next(600, now) == scheduler.now_str(now + 600)
    assert scheduler.compute_next(1, now) == scheduler.now_str(now + scheduler.MIN_INTERVAL)
    assert scheduler.compute_end(0, now) is None
    assert scheduler.compute_end(7 * 86400, now) == scheduler.now_str(now + 7 * 86400)
    assert scheduler.parse_time('2026-01-02 03:04:05') is not None
    assert scheduler.parse_time('') is None and scheduler.parse_time('乱码') is None

    # 5) API：非法参数 400，合法创建后能在列表里看到人话化的摘要
    t = db.create_machine({'name': 'sch-t', 'role': 'target', 'region': '香港',
                           'bandwidth': '500M'})
    b = db.create_machine({'name': 'sch-b', 'role': 'backend', 'region': '东京',
                           'bandwidth': '200M'})
    c = flask_app.test_client()
    _login(c)
    r = c.post('/api/schedules', json={'target_id': t['id'], 'backend_ids': [b['id']],
                                       'interval_seconds': 60})
    assert r.status_code == 400 and '间隔时间' in r.get_json()['error'], r.get_json()
    r = c.post('/api/schedules', json={'target_id': t['id'], 'backend_ids': [b['id']],
                                       'duration': 0})
    assert r.status_code == 400 and '时长' in r.get_json()['error'], r.get_json()
    with patch.object(scheduler, 'trigger', return_value=(True, '已启动测试 #999')) as start_mock:
        r = c.post('/api/schedules', json={'target_id': t['id'], 'backend_ids': [b['id']],
                                           'name': '长期对比', 'interval_seconds': 600,
                                           'lifetime_seconds': 7 * 86400,
                                           'streams': 2, 'duration': 5, 'ping_count': 20,
                                           'udp': True, 'udp_bandwidth': '50M'})
        assert start_mock.called, '新建定时任务保存后必须立即触发首轮'
    assert r.status_code == 200, r.get_json()
    got = r.get_json()
    assert got['interval_text'] == '10\u00a0分钟' and got['backend_count'] == 1, got
    assert got['target_name'] == 'sch-t' and got['backend_names'] == ['sch-b'], got
    assert got['udp'] == 1 and got['streams'] == 2 and got['ping_count'] == 20, got
    assert got['lifetime_text'] == '7\u00a0天' and got['ends_at'], got
    assert got['start_attempted'] is True and got['started'] is True, got
    assert got['start_note'] == '已启动测试 #999', got
    assert got['next_run_in'], got                       # 菜单上要显示「X 分钟后执行」
    sid = got['id']
    lst = c.get('/api/schedules').get_json()
    assert any(x['id'] == sid for x in lst), lst

    # 6) 改间隔要重排下一次；改名字不能把周期重置掉
    before = db.get_schedule(sid)['next_run_at']
    r = c.put(f'/api/schedules/{sid}', json={
        'target_id': t['id'], 'backend_ids': [b['id']], 'name': '长期对比-改名',
        'interval_seconds': 600, 'streams': 2, 'duration': 5, 'ping_count': 20,
        'udp': True, 'udp_bandwidth': '50M'})
    assert r.status_code == 200, r.get_json()
    assert db.get_schedule(sid)['next_run_at'] == before, '同名同间隔的编辑不该重置周期'
    assert db.get_schedule(sid)['lifetime_seconds'] == 7 * 86400
    r = c.put(f'/api/schedules/{sid}', json={
        'target_id': t['id'], 'backend_ids': [b['id']], 'name': '长期对比-改名',
        'interval_seconds': 1800, 'streams': 2, 'duration': 5, 'ping_count': 20})
    assert r.status_code == 200, r.get_json()
    assert db.get_schedule(sid)['next_run_at'] != before, '改了间隔应该重排下一次'

    # 7) 部分更新只改名，不能把测试参数重置为默认值
    r = c.put(f'/api/schedules/{sid}', json={'name': '只改名字'})
    assert r.status_code == 200, r.get_json()
    preserved = db.get_schedule(sid)
    assert (preserved['streams'], preserved['duration'], preserved['ping_count'],
            preserved['udp'], preserved['udp_bandwidth']) == (2, 5, 20, 1, '50M'), preserved

    # 8) 停用 / 启用 / 删除
    r = c.post(f'/api/schedules/{sid}/toggle')
    assert r.status_code == 200 and r.get_json()['enabled'] is False, r.get_json()
    assert db.due_schedules('2099-01-01 00:00:00') == [] or all(
        x['id'] != sid for x in db.due_schedules('2099-01-01 00:00:00'))
    r = c.post(f'/api/schedules/{sid}/toggle')
    assert r.get_json()['enabled'] is True, r.get_json()
    assert c.get(f'/api/schedules/{sid}').status_code == 200
    assert c.delete(f'/api/schedules/{sid}').status_code == 200
    assert c.get(f'/api/schedules/{sid}').status_code == 404
    print('定时任务字段校验 / API / 周期重排 OK')


def test_schedule_lifetime_expiry():
    """达到运行时长后自动停止；延长时长后可以恢复，不丢历史。"""
    from app import db, scheduler
    db.init_db()
    t = db.create_machine({'name': 'life-t', 'role': 'target', 'region': '', 'bandwidth': ''})
    b = db.create_machine({'name': 'life-b', 'role': 'backend', 'region': '', 'bandwidth': ''})
    fields = scheduler.normalize_schedule({
        'target_id': t['id'], 'backend_ids': [b['id']], 'name': '到期用例',
        'interval_seconds': 600, 'lifetime_seconds': 3600,
    })
    fields['ends_at'] = '2000-01-01 00:00:00'
    fields['next_run_at'] = '2000-01-01 00:00:00'
    sch = db.create_schedule(fields)

    assert sch['id'] in db.expire_schedules()
    got = db.get_schedule(sch['id'])
    assert got['enabled'] is False and got['last_status'] == 'expired', got
    assert got['next_run_at'] is None and '运行时长' in got['last_error'], got
    assert all(x['id'] != sch['id'] for x in db.due_schedules('2099-01-01 00:00:00'))
    ok, note = scheduler.trigger(sch['id'], manual=True)
    assert not ok and '运行时长' in note, note

    # 延长后 ends_at 在未来，仍需要用户启用；启用后重新排期
    db.update_schedule(sch['id'], {'ends_at': scheduler.compute_end(3600)})
    db.update_schedule(sch['id'], {'enabled': 1,
                                   'next_run_at': scheduler.compute_next(600)})
    got = db.get_schedule(sch['id'])
    assert got['enabled'] is True and not scheduler.is_expired(got), got
    db.delete_schedule(sch['id'])
    print('定时任务运行时长 / 到期自动停止 / 恢复 OK')


def test_schedule_trigger_flow():
    """触发一轮：正常启动、与手动测试冲突时跳过并说明原因、等待模式不丢轮次。"""
    from app import db, runner, scheduler
    db.init_db()
    t = db.create_machine({'name': 'trig-t', 'role': 'target', 'region': '', 'bandwidth': ''})
    b = db.create_machine({'name': 'trig-b', 'role': 'backend', 'region': '', 'bandwidth': ''})
    db.touch_machine(t['id'], 'trig-t', '93.184.216.34')
    db.touch_machine(b['id'], 'trig-b', '93.184.216.34')

    sch = db.create_schedule(dict(
        scheduler.normalize_schedule({'target_id': t['id'], 'backend_ids': [b['id']],
                                      'name': '触发用例', 'interval_seconds': 600}),
        next_run_at=scheduler.compute_next(600)))

    # 1) 正常触发：真正走到「建测试记录」这一步（真实代码路径），只是不启动执行线程
    started = {}
    real_start_run = runner.start_run

    def fake_start_run(target_id, backend_ids, ip_version=4, params=None,
                       source='manual', schedule_id=None, label=''):
        started.update(target_id=target_id, backend_ids=backend_ids, ip_version=ip_version,
                       params=params, source=source, schedule_id=schedule_id, label=label)
        # 不启动执行线程，避免真去连 Agent；但记录本身要真的建出来，
        # 否则 finish_pending 会因为「测试记录不存在」把这一轮算成失败
        return db.create_run(db.get_machine(target_id), backend_ids, ip_version,
                             target_host='93.184.216.34', source=source,
                             schedule_id=schedule_id, **params)

    runner.start_run = fake_start_run
    try:
        ok, note = scheduler.trigger(sch['id'])
        assert ok, note
        assert started['source'] == 'schedule' and started['schedule_id'] == sch['id'], started
        assert started['backend_ids'] == [b['id']] and started['params']['port'] == 5201, started
    finally:
        runner.start_run = real_start_run
    s = db.get_schedule(sch['id'])
    assert s['last_status'] == 'running' and s['last_run_id'], s
    assert s['next_run_at'] > scheduler.now_str(), s           # 触发后立刻推下一次，避免重复触发
    assert db.get_schedule_runs(sch['id'])[0]['run_id'] == s['last_run_id']

    # 2) 完成回填：run 跑完后把状态写回定时任务与这一轮
    rid = s['last_run_id']
    db.update_run(rid, status='finished')
    db.update_schedule(sch['id'], {'last_status': 'running'})
    scheduler.finish_pending()
    s = db.get_schedule(sch['id'])
    assert s['last_status'] == 'finished' and s['run_count'] == 1, s
    assert s['last_error'] == '', s
    sr = [x for x in db.get_schedule_runs(sch['id']) if x['run_id'] == rid][0]
    assert sr['status'] == 'finished', sr

    # 2b) 回填必须幂等：同一轮不能被计两次（轮次记录一旦收尾就不再是 pending）
    before = db.get_schedule(sch['id'])['run_count']
    scheduler.finish_pending()
    scheduler.finish_pending()
    s = db.get_schedule(sch['id'])
    assert s['run_count'] == before and s['fail_count'] == 0, s
    assert [x for x in db.pending_schedule_runs() if x['schedule_id'] == sch['id']] == []

    # 2c) 跳过 / 等待之后 last_run_id 会指向更早那一轮，不能被当成待回填的轮次
    db.update_schedule(sch['id'], {'last_status': 'waiting'})
    scheduler.finish_pending()
    assert db.get_schedule(sch['id'])['run_count'] == before, db.get_schedule(sch['id'])

    # 3) 与手动测试冲突：默认跳过本轮，且原因写在面板能看到的地方
    runner._active[999999] = {'stop': False, 'log': []}      # 模拟「有测试在跑」
    try:
        ok, note = scheduler.trigger(sch['id'])
        assert not ok and '已有测试' in note, note
    finally:
        runner._active.pop(999999, None)
    s = db.get_schedule(sch['id'])
    assert s['last_status'] == 'skipped' and s['skip_count'] == 1, s
    assert '已有测试' in s['last_error'], s
    sr = db.get_schedule_runs(sch['id'])[0]
    assert sr['status'] == 'skipped' and sr['run_id'] is None, sr

    # 4) on_busy=wait：不记跳过，改成过一会儿再看（同一轮不会丢）
    db.update_schedule(sch['id'], {'on_busy': 'wait', 'skip_count': 0})
    runner._active[999998] = {'stop': False, 'log': []}
    try:
        ok, note = scheduler.trigger(sch['id'])
        assert not ok and '等待' in note, note
    finally:
        runner._active.pop(999998, None)
    s = db.get_schedule(sch['id'])
    assert s['last_status'] == 'waiting' and s['skip_count'] == 0, s
    assert 0 < scheduler.parse_time(s['next_run_at']) - time.time() <= 300, s['next_run_at']

    # 5) 机器被删除 / 角色被改：跳过并给出可读原因
    db.delete_machine(b['id'])
    ok, note = scheduler.trigger(sch['id'])
    assert not ok and '已被删除' in note, note
    assert '已被删除' in db.get_schedule(sch['id'])['last_error']
    print('定时任务触发 / 冲突跳过 / 等待 / 机器失联 OK')


def test_schedule_restart_recovery():
    """面板重启：定时任务不能丢，正在跑的那一轮标中断，下次时间保持不变。"""
    from app import db, scheduler
    db.init_db()
    t = db.create_machine({'name': 'rc-t', 'role': 'target', 'region': '', 'bandwidth': ''})
    b = db.create_machine({'name': 'rc-b', 'role': 'backend', 'region': '', 'bandwidth': ''})
    sch = db.create_schedule(dict(
        scheduler.normalize_schedule({'target_id': t['id'], 'backend_ids': [b['id']],
                                      'name': '重启用例', 'interval_seconds': 600}),
        next_run_at='2026-01-01 00:00:00', last_status='running', last_run_id=777))
    db.record_schedule_run(sch['id'], 777, '2026-01-01 00:00:00', status='running')

    db.mark_stale_runs()                      # 等价于面板进程重启时的自愈
    scheduler.finish_pending()
    s = db.get_schedule(sch['id'])
    assert s['enabled'] == 1, s
    assert s['last_status'] == 'interrupted', s
    assert s['next_run_at'] == '2026-01-01 00:00:00', '重启不应改动排期'
    sr = db.get_schedule_runs(sch['id'])[0]
    assert sr['status'] == 'interrupted', sr

    # 已经过点的任务会在下一次 tick 被选中（重启后继续按原计划跑）
    assert any(x['id'] == sch['id'] for x in db.due_schedules()), db.due_schedules()

    # 被中断的轮次要计入失败，并且不能出现「完成 N 轮却一次都没跑成」的错账
    rid = db.create_run(t, [b['id']], 4, target_host='93.184.216.34', source='schedule',
                        schedule_id=sch['id'], port=5201, streams=1, duration=1,
                        udp=False, udp_bandwidth='100M', ping_count=1)
    db.record_schedule_run(sch['id'], rid, scheduler.now_str(), status='running')
    db.mark_stale_runs()                       # 面板重启：这一轮被判定中断，
    #                                           记账也在这一步完成（一旦标成 interrupted
    #                                           就没有「待回填」的入口了，不在这里记就会漏轮次）
    scheduler.finish_pending()
    s = db.get_schedule(sch['id'])
    sr = [x for x in db.get_schedule_runs(sch['id']) if x['run_id'] == rid][0]
    assert sr['status'] == 'interrupted', sr
    # 记账从轮次记录重算：这个任务历史上有两轮（初始那轮 + 这一轮），两轮都是中断
    assert s['run_count'] == 2 and s['fail_count'] == 2, s
    assert '中断' in s['last_error'], s
    # 幂等：再重启一次计数不会继续涨
    db.mark_stale_runs()
    s2 = db.get_schedule(sch['id'])
    assert s2['run_count'] == s['run_count'] and s2['fail_count'] == s['fail_count'], s2
    db.delete_schedule(sch['id'])
    print('定时任务重启恢复 OK')


def test_schedule_comparison_report():
    """长期对比：跨轮汇总、趋势计算、Markdown 报告（含脱敏）。"""
    from app import db, quality, scheduler
    db.init_db()
    t = db.create_machine({'name': 'cmp-t', 'role': 'target', 'region': '香港',
                           'bandwidth': '1G'})
    b = db.create_machine({'name': 'cmp-b', 'role': 'backend', 'region': '东京',
                           'bandwidth': '500M'})
    db.touch_machine(t['id'], 'cmp-t', '93.184.216.34')     # 报告里目标机地址要能取到并脱敏
    sch = db.create_schedule(dict(
        scheduler.normalize_schedule({'target_id': t['id'], 'backend_ids': [b['id']],
                                      'name': '对比用例', 'interval_seconds': 600,
                                      'duration': 10}),
        next_run_at=scheduler.compute_next(600)))

    # 造 6 轮：上行从 100 逐步掉到 70，检验趋势为下滑
    for i, up in enumerate((100.0, 98.0, 96.0, 82.0, 76.0, 70.0)):
        rid = db.create_run(t, [b['id']], 4, target_host='93.184.216.34', source='schedule',
                            schedule_id=sch['id'], port=5201, streams=1, duration=10,
                            udp=False, udp_bandwidth='100M', ping_count=10)
        mt_up = (f'[ ID] Interval           Transfer     Bitrate         Retr\n'
                 f'[  5]   0.00-10.00  sec   100 MBytes  {up} Mbits/sec    0             sender\n'
                 f'[  5]   0.00-10.10  sec   100 MBytes  {up} Mbits/sec                  receiver\n')
        mt_down = (f'[ ID] Interval           Transfer     Bitrate         Retr\n'
                   f'[  5]   0.00-10.10  sec   100 MBytes  {up + 10} Mbits/sec    0             sender\n'
                   f'[  5]   0.00-10.00  sec   100 MBytes  {up + 8} Mbits/sec                  receiver\n')
        metrics = quality.parse_metrics(PING_RAW, mt_up, mt_down)
        quality.evaluate(metrics, b)
        db.update_item(db.get_run_items(rid)[0]['id'], status='done', phase='完成',
                       metrics=json.dumps(metrics))
        db.update_run(rid, status='finished')
        db.record_schedule_run(sch['id'], rid, scheduler.now_str(), status='finished')
        db.finish_schedule_run(sch['id'], rid, 'finished')

    data = scheduler.comparison_data(sch['id'])
    assert len(data['rounds']) == 6, data['rounds']
    m = data['machines'][0]
    assert m['name'] == 'cmp-b' and len(m['series']) == 6, m
    st = data['stats'][0]
    assert st['samples'] == 6 and abs(st['up']['min'] - 70.0) < 1e-6, st
    assert abs(st['up']['max'] - 100.0) < 1e-6 and abs(st['up']['avg'] - (100 + 98 + 96 + 82 + 76 + 70) / 6) < 1e-6, st
    assert st['trend_pct'] is not None and st['trend_pct'] < -3, st   # 明显下滑
    assert st['trend_reason'] is None, st

    view = scheduler.comparison_data(sch['id'], view_limit=3)
    assert len(view['rounds']) == 3 and view['total_rounds'] == 6, view['total_rounds']
    assert view['stats'][0]['samples'] == 6, '页面窗口不能截断全量统计'

    rep = scheduler.build_comparison_report(sch['id'])
    assert '# iperf3 长期对比报告' in rep, rep
    assert '对比用例' in rep and 'cmp-b' in rep and '趋势' in rep, rep
    assert '| 机器 | 地区 | 样本 | 上行 均/低/高 | 下行 均/低/高' in rep, rep
    assert '最低' not in rep.split('## 线路表现', 1)[1].split('## ', 1)[0], rep
    assert '93.184.*.*' in rep and '216.34' not in rep, rep         # 报告脱敏
    assert '每 10\u00a0分钟' in rep, rep
    assert '已完成轮次' in rep and '成功 6' in rep, rep
    assert '| 运行时长 | 不限 · 不会自动停止 |' in rep, rep
    assert scheduler._md_cell('a|b\nc') == 'a\\|b c'
    assert scheduler.ROUND_LIMIT >= 10000, scheduler.ROUND_LIMIT

    # 删除定时任务后，历史轮次与对比图仍必须可访问
    db.delete_schedule(sch['id'])
    assert db.get_schedule(sch['id']) is None
    assert all(x['id'] != sch['id'] for x in db.get_schedules())
    assert any(x['id'] == sch['id'] and x['archived'] for x in db.get_schedules(include_archived=True))
    archived = scheduler.comparison_data(sch['id'])
    assert archived and archived['schedule']['archived'] is True, archived
    assert len(archived['rounds']) == 6, archived
    from app.app import app as flask_app
    c = flask_app.test_client()
    _login(c)
    r = c.get(f"/api/schedules/{sch['id']}/comparison")
    assert r.status_code == 200 and len(r.get_json()['rounds']) == 6, r.get_json()
    r = c.get(f"/api/schedules/{sch['id']}/report")
    assert r.status_code == 200 and '历史归档' in r.get_data(as_text=True), r.status_code

    short = db.create_schedule(dict(
        scheduler.normalize_schedule({'target_id': t['id'], 'backend_ids': [b['id']],
                                      'name': '两轮趋势用例'}),
        next_run_at='2026-01-01 00:00:00'))
    for up_value in (100.0, 101.0):
        rid = db.create_run(t, [b['id']], 4, target_host='93.184.216.34',
                            source='schedule', schedule_id=short['id'], port=5201,
                            streams=1, duration=10, udp=False,
                            udp_bandwidth='100M', ping_count=10)
        up_raw = (f'[SUM]   0.00-10.00  sec   100 MBytes   {up_value} Mbits/sec   0             sender\n'
                  f'[SUM]   0.00-10.10  sec   100 MBytes   {up_value} Mbits/sec                  receiver')
        mt_short = quality.parse_metrics(PING_RAW, up_raw, DOWN_RAW)
        quality.evaluate(mt_short, b, {'duration': 10})
        db.update_item(db.get_run_items(rid)[0]['id'], status='done', metrics=json.dumps(mt_short))
        db.update_run(rid, status='finished')
        db.record_schedule_run(short['id'], rid, scheduler.now_str(), status='finished')
        db.finish_schedule_run(short['id'], rid, 'finished')
    short_data = scheduler.comparison_data(short['id'])
    assert short_data['stats'][0]['trend_pct'] is None, short_data
    assert '样本不足 4 轮' in short_data['stats'][0]['trend_reason'], short_data

    # 空数据不能炸
    empty = db.create_schedule(dict(
        scheduler.normalize_schedule({'target_id': t['id'], 'backend_ids': [b['id']],
                                      'name': '空用例'}), next_run_at='2026-01-01 00:00:00'))
    rep2 = scheduler.build_comparison_report(empty['id'])
    assert '还没有完成任何一轮测试' in rep2, rep2
    assert scheduler.comparison_data(999999) is None
    print('长期对比数据 / 趋势 / Markdown 报告 OK')


def test_incomplete_direction_is_failure():
    """上下行任一方向无有效吞吐时，整台机器按失败处理，不进入统计。"""
    from app import db, quality, runner
    db.init_db()
    t = db.create_machine({'name': 'half-t', 'role': 'target', 'region': '', 'bandwidth': ''})
    b = db.create_machine({'name': 'half-b', 'role': 'backend', 'region': '', 'bandwidth': ''})
    rid = db.create_run(t, [b['id']], 4, target_host='93.184.216.34', source='schedule',
                        schedule_id=None, port=5201, streams=1, duration=10,
                        udp=False, udp_bandwidth='100M', ping_count=1)
    metrics = quality.parse_metrics(PING_RAW, UP_RAW, DOWN_RAW)
    metrics['down_mbits'] = None
    iid = db.get_run_items(rid)[0]['id']
    db.update_item(iid, status='done', metrics=json.dumps(metrics))
    assert db.repair_incomplete_items() >= 1
    item = db.get_run_item(iid)
    assert item['status'] == 'failed' and '下行' in item['error'], item
    assert item['metrics'] == ''
    assert db.get_run(rid)['status'] == 'failed'
    total, done, failed = db.run_counts(rid)
    assert (total, done, failed) == (1, 0, 1), (total, done, failed)

    assert runner.bidirectional_failure({'up_mbits': 1, 'down_mbits': None})
    assert runner.bidirectional_failure({'up_mbits': None, 'down_mbits': 1})
    assert runner.bidirectional_failure({'up_mbits': 0, 'down_mbits': 1})
    assert runner.bidirectional_failure({'up_mbits': 1, 'down_mbits': 1}) == ''
    print('单边吞吐结果按整机失败处理 OK')


def test_active_job_keeps_machine_online():
    """Agent 阻塞执行长任务时，活动任务应覆盖心跳超时造成的离线误判。"""
    from app import db
    db.init_db()
    m = db.create_machine({'name': 'busy', 'role': 'backend', 'region': '', 'bandwidth': ''})
    db.get_db().execute('UPDATE machines SET last_seen=0 WHERE id=?', (m['id'],))
    db.get_db().commit()
    assert db.get_machine(m['id'])['online'] is False
    jid = db.create_job(m['id'], 'ping', {'host': '93.184.216.34', 'count': 1}, 30)
    assert db.next_queued_job(m['id'])['id'] == jid
    got = db.get_machine(m['id'])
    assert got['online'] is True and got['active_job'] is True, got
    db.finish_job(jid, 0)
    db.get_db().execute('UPDATE machines SET last_seen=0 WHERE id=?', (m['id'],))
    db.get_db().commit()
    assert db.get_machine(m['id'])['online'] is False
    print('活动任务期间机器在线状态 OK')


def test_security_hardening():
    """Registration codes, structured jobs, CSP, port isolation and file permissions."""
    import base64
    import json as _json
    import stat
    import app.app as app_mod
    from app import db, job_types
    db.init_db()
    c = app_mod.app.test_client()
    _login(c)

    m = db.create_machine({
        'name': 'security-agent', 'role': 'backend', 'region': '', 'bandwidth': '',
    })
    public = c.get(f'/api/machines/{m["id"]}').get_json()
    assert 'token' not in public and 'sign_key' not in public, public

    code_resp = c.post(f'/api/machines/{m["id"]}/registration-code')
    assert code_resp.status_code == 200, code_resp.get_data(as_text=True)
    reg = code_resp.get_json()
    assert len(reg['code']) == 10 and reg['expires_in'] == 600, reg
    stored = db.get_db().execute(
        'SELECT code_hash FROM agent_reg_codes WHERE machine_id=? ORDER BY expires_at DESC',
        (m['id'],)).fetchone()
    assert stored and stored['code_hash'] != reg['code'], stored

    old_allow = app_mod.ALLOW_INSECURE_AGENT
    app_mod.ALLOW_INSECURE_AGENT = True
    try:
        enrolled = c.post('/api/agent/register', json={'code': reg['code']})
        assert enrolled.status_code == 200, enrolled.get_data(as_text=True)
        secrets_json = enrolled.get_json()
        assert len(secrets_json['token']) == 64
        assert len(secrets_json['sign_key']) == 64
        assert c.post('/api/agent/register', json={'code': reg['code']}).status_code == 403
    finally:
        app_mod.ALLOW_INSECURE_AGENT = old_allow

    # Structured jobs contain no free-form shell and are signed as canonical payloads.
    assert job_types.encode('ping', {'host': '93.184.216.34', 'count': 2}) == (
        'kind=ping\ncount=2\nhost=93.184.216.34\n')
    for bad in (
        ('ping', {'host': '1.2.3.4;id', 'count': 1}),
        ('iperf_client', {'host': '1.2.3.4', 'port': 1, 'duration': 1,
                          'streams': 1, 'udp': False, 'udp_bandwidth': '1M',
                          'reverse': False, 'cmd': 'x'}),
        ('unknown_shell', {}),
    ):
        try:
            job_types.normalize(*bad)
            raise AssertionError(f'应拒绝非法任务：{bad}')
        except ValueError:
            pass

    jid = db.create_job(m['id'], 'ping', {'host': '93.184.216.34', 'count': 2}, 10)
    hb = c.post('/api/agent/heartbeat', headers={
        'X-Agent-Token': secrets_json['token'],
        'X-Agent-Protocol': '2',
        'X-Agent-Host': 'security-agent',
    }, environ_base={'REMOTE_ADDR': '127.0.0.1'})
    assert hb.status_code == 200, hb.get_data(as_text=True)
    fields = dict(line.split('=', 1) for line in hb.get_data(as_text=True).splitlines()
                  if '=' in line)
    assert fields['job_id'] == str(jid) and fields['kind'] == 'ping', fields
    payload = base64.b64decode(fields['payload_b64']).decode()
    assert payload == job_types.encode('ping', {'host': '93.184.216.34', 'count': 2})

    done = c.post('/api/agent/output', headers={'X-Agent-Token': secrets_json['token']},
                  data={'job_id': jid, 'text': 'ok', 'done': '1', 'exit_code': '0'})
    assert done.status_code == 200
    late = c.post('/api/agent/output', headers={'X-Agent-Token': secrets_json['token']},
                  data={'job_id': jid, 'text': 'tamper', 'done': '1', 'exit_code': '0'})
    assert late.status_code == 410

    # 旧协议心跳不能继续显示在线或挂住任务
    old_m = db.create_machine({'name': 'old-agent', 'role': 'backend',
                               'region': '', 'bandwidth': ''})
    old_job = db.create_job(
        old_m['id'], 'report_ips', {}, 10)
    old_hb = c.post('/api/agent/heartbeat', headers={
        'X-Agent-Token': old_m['token'],
        'X-Agent-Host': 'old-agent',
    }, environ_base={'REMOTE_ADDR': '127.0.0.1'})
    assert 'upgrade_required' in old_hb.get_data(as_text=True)
    old_state = db.get_machine(old_m['id'])
    assert old_state['online'] is False and old_state['last_seen'] == 0, old_state
    assert '协议版本过旧' in old_state['probe_error'], old_state
    assert db.get_job(old_job)['status'] == 'failed', db.get_job(old_job)

    login_page = app_mod.app.test_client().get('/login')
    csp = login_page.headers.get('Content-Security-Policy', '')
    assert "script-src 'self' 'nonce-" in csp and 'frame-ancestors none' not in csp
    assert 'frame-ancestors \'none\'' in csp
    assert 'nonce="' in login_page.get_data(as_text=True)

    isolated = c.get(
        '/api/machines',
        base_url=f'http://127.0.0.1:{app_mod.AGENT_INTERNAL_PORT}')
    assert isolated.status_code == 404, isolated.get_data(as_text=True)

    for path in (db.DB_PATH, db.SECRET_PATH):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode & 0o077 == 0, (path, oct(mode))

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'app', 'agent', 'install.sh'), encoding='utf-8') as f:
        agent_installer = f.read()
    for needle in ('X-Agent-Protocol: 2', 'iperf3 -s -B "$chosen"',
                   'NoNewPrivileges=true', 'User=linkpilot'):
        assert needle in agent_installer, needle
    assert 'bash "$SPOOL/job.sh"' not in agent_installer
    assert 'PANEL_URL=\'$PANEL_URL\'' not in agent_installer

    with open(os.path.join(root, 'Caddyfile.acme'), encoding='utf-8') as f:
        caddy_acme = f.read()
    assert 'X-LinkPilot-Proxy-Secret' in caddy_acme
    assert 'header_up -CF-Connecting-IP' in caddy_acme
    assert 'X-Forwarded-For {http.request.header.CF-Connecting-IP}' not in caddy_acme
    with open(os.path.join(root, 'docker-compose.yml'), encoding='utf-8') as f:
        compose = f.read()
    assert '${PANEL_BIND:-127.0.0.1}' in compose
    assert '${PANEL_AGENT_BIND:-0.0.0.0}' in compose
    with open(os.path.join(root, 'Dockerfile'), encoding='utf-8') as f:
        dockerfile = f.read()
    assert 'USER 10001:10001' in dockerfile
    print('注册码 / 结构化任务 / CSP / 管理面隔离 / 权限 OK')


def test_dashboard_interactions_present():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'app', 'templates', 'index.html'), encoding='utf-8') as f:
        html = f.read()
    for key in ('setChartMode', 'toggleChartMachine', 'setChartDirection',
                'buildTrendChartSvg', 'copyRichText', 'toggleRunGroup', 'run-group',
                'AbortController', 'skeleton-chart', 'comparisonTableHtml', '生成报告中',
                '@media (max-width:700px)', 'params-grid', '保存后立即开始首轮',
                'showPopconfirm', 'eventStream', 'runSummary', 'ratingCellHtml',
                'ratingExplanationHtml', 'ratingText',
                'renderRunEvents', 'rating_diagnostics', 'event-stage',
                'chart-control-label', '<th>上行</th><th>下行</th>',
                'run-form', 'run-section-label', 'run-submit',
                '<th>上行 均/低/高</th><th>下行 均/低/高</th>',
                'ratingText', 'machines-table', 'col-machine', 'machine-actions',
                'table-layout:fixed', 'function fmtN(', 'fmtN(agg.avg, nd)',
                'function renderRuns(', 'LAST_MACHINES_SIG',
                'data-action="toggleSchedCard"',
                'document.startViewTransition', 'themeCircleReveal',
                'revealThemeWithSnapshot', 'theme-reveal-overlay',
                'data-machine-id', 'machine-drag-ghost',
                'machine-row-shifting', '/api/machines/order',
                'requestAnimationFrame(machineDragFrame)',
                "addEventListener('touchmove'",
                "addEventListener('pointermove'"):
        assert key in html, f'缺少面板交互：{key}'
    assert "confirm('确定删除该测试记录？')" not in html, '测试记录删除不应使用原生确认框'
    assert 'alert(' not in html and 'prompt(' not in html and \
        not re.search(r'(?<!Pop)confirm\s*\(', html), \
        '面板不应再使用浏览器原生 alert/confirm/prompt'
    assert 'data-pop="confirm"' in html, 'Popconfirm 缺少确认按钮'
    regen_block = html.split('function regenToken(', 1)[1].split('function deleteMachine', 1)[0]
    assert 'showPopconfirm(anchor' in regen_block, '新接入码应使用 Popconfirm'
    stop_block = html.split('function stopCurrentRun(', 1)[1].split('let activePopconfirm', 1)[0]
    assert 'showPopconfirm(anchor' in stop_block, '停止测试应使用 Popconfirm'
    start_block = html.split('function startRun(anchor)', 1)[1].split('/* ---------------- 测试记录', 1)[0]
    assert 'if (!confirm(' not in start_block and "confirm('" not in start_block, \
        '开始测试不应使用原生确认框'
    assert 'showPopconfirm(anchor' in start_block, '开始测试应使用 Popconfirm'
    assert '上行 (sender)' not in html and '下行 (receiver)' not in html
    assert '最低 ${fmt' not in html, '跨轮汇总不应再用换行堆叠最低/最高说明'
    assert 'fmt(agg.avg, nd)' not in html, '小数位参数不能被当作字符串后缀拼接'
    for fn_name, end_marker in (
        ('function saveSchedule(anchor)', 'function editSchedule'),
        ('function toggleSchedule(id, anchor)', 'function deleteSchedule'),
        ('function deleteSchedule(id, anchor)', 'function runScheduleNow'),
        ('function runScheduleNow(id, anchor)', '/* --- 对比视图 --- */'),
    ):
        block = html.split(fn_name, 1)[1].split(end_marker, 1)[0]
        assert '!confirm(' not in block, f'{fn_name} 仍在使用原生确认框'
        assert 'showPopconfirm(anchor' in block, f'{fn_name} 应使用 Popconfirm'
    for removed in ('默认 IPv4：iperf3 与 ping 都使用目标机的 IPv4 地址',
                    '参数即 iperf3 原生命令'):
        assert removed not in html, f'冗长说明未移除：{removed}'
    print('图表控制 / 记录折叠 / 报告富文本复制 / 精简说明 OK')


def test_event_stream_log_contract():
    """事件流与原始日志分离，原始输出不能被丢弃。"""
    from app import runner
    rid = 987654321
    runner._active[rid] = {
        'stop': False,
        'log': [
            '[12:00:00] raw ping output',
            '@@EVENT@@' + __import__('json').dumps({
                'time': '12:00:01', 'stage': 'ping', 'status': 'done',
                'title': 'Ping 完成', 'summary': '丢包 0%', 'machine': 'b1',
            }, ensure_ascii=False),
            '[12:00:02] raw iperf output',
        ],
    }
    try:
        data = runner.get_log_data(rid)
        assert data['events'][0]['stage'] == 'ping', data
        assert data['raw_lines'] == ['[12:00:00] raw ping output',
                                     '[12:00:02] raw iperf output'], data
        assert runner.get_log(rid) == data['raw_lines'], data
    finally:
        runner._active.pop(rid, None)
    print('结构化事件 / 原始日志分离 OK')


def test_run_list_returns_summary_only():
    """测试记录列表不能携带完整日志和报告，否则自动刷新会传输数 MB。"""
    from app import db
    from app.app import app as flask_app
    db.init_db()
    t = db.create_machine({'name': 'list-t', 'role': 'target', 'region': '', 'bandwidth': ''})
    b = db.create_machine({'name': 'list-b', 'role': 'backend', 'region': '', 'bandwidth': ''})
    rid = db.create_run(t, [b['id']], 4, target_host='93.184.216.34',
                        port=5201, streams=1, duration=1, udp=False,
                        udp_bandwidth='100M', ping_count=1)
    db.update_run(rid, status='finished', report='R' * 200000, log='L' * 200000)
    rows = db.get_runs()
    row = next(r for r in rows if r['id'] == rid)
    assert 'log' not in row and 'report' not in row, row.keys()

    c = flask_app.test_client()
    _login(c)
    resp = c.get('/api/runs')
    assert resp.status_code == 200
    assert len(resp.get_data()) < 50000, len(resp.get_data())
    detail = c.get(f'/api/runs/{rid}').get_json()
    assert detail['run']['report'].startswith('## LinkPilot 测试结果'), detail['run']['report'][:80]
    assert '诊断详情' not in detail['run']['report'] and '### 原始数据' in detail['run']['report']
    report_resp = c.get(f'/api/runs/{rid}/report')
    assert report_resp.status_code == 200
    assert '## LinkPilot 测试结果' in report_resp.get_data(as_text=True)
    print('测试记录列表摘要接口体积 OK')


def test_version_sync():
    """版本号必须写在一处：app.py 是唯一权威，install.sh 里的值只用于「全新安装」开场。

    这三处一旦漂移，用户就会看到「部署脚本说 vX，面板里显示 vY」，所以用测试钉住。
    """
    import re as _re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def read(path):
        with open(os.path.join(root, path), encoding='utf-8') as f:
            return f.read()

    app_py = read('app/app.py')
    m = _re.search(r"^APP_VERSION\s*=\s*'([^']+)'", app_py, _re.M)
    assert m, 'app/app.py 里找不到 APP_VERSION'
    ver = m.group(1)
    assert _re.match(r'^\d+\.\d+\.\d+$', ver), ver
    # 测试阶段用 0.x 版本
    assert ver.startswith('0.'), f'测试阶段应保持 0.x 版本，当前 {ver}'

    inst = read('install.sh')
    m2 = _re.search(r'^APP_VERSION="([^"]+)"', inst, _re.M)
    assert m2, 'install.sh 里找不到 APP_VERSION'
    assert m2.group(1) == ver, f'install.sh({m2.group(1)}) 与 app.py({ver}) 版本不一致'

    # 面板横幅不再自己维护版本，而是从 app.py 读
    banner = read('banner.py')
    assert 'def app_version()' in banner, 'banner.py 应复用 app.py 的版本号'
    assert "APP_VERSION" in banner and 're.search' in banner, banner[:200]

    # 自动更新的关键实现必须在场：检查更新 / 强制对齐 / 用新脚本重跑
    for key in ('LINKPILOT_REEXEC', 'LINKPILOT_UPDATED',
                'checkout -q -B "$BRANCH" "origin/$BRANCH"',
                'LINKPILOT_DRY_RUN'):
        assert key in inst, f'install.sh 缺少自动更新关键片段：{key}'
    assert 'git pull --ff-only' not in inst, \
        '不该再用 git pull --ff-only：远端历史被重写（force push）时会直接失败'
    assert 'stash push --include-untracked' in inst, \
        '本地有改动时应先备份到 stash，再自动更新'
    assert 'ensure_runtime_excludes' in inst, \
        '升级前必须把 .env / 证书 / 数据排除在本地代码改动之外'
    assert 'restore_runtime_from_backup' in inst, \
        '旧版脚本已 stash 运行时文件时，新版必须能自动恢复'
    assert 'LINKPILOT_DIR' in inst and 'LEGACY_UPGRADE' not in inst, \
        '安装目录应只使用 LinkPilot 配置，不保留旧项目迁移逻辑'
    for key in ('tls_config_present', 'PANEL_DOMAIN=.+', 'CADDY_HTTPS=.+'):
        assert key in inst, f'HTTPS 自动识别缺少关键片段：{key}'
    ignore = read('.gitignore')
    assert '\n.env\n' in '\n' + ignore, '.env 必须被 .gitignore 忽略'
    assert '\ncerts/\n' in '\n' + ignore, 'certs/ 必须被 .gitignore 忽略'
    print(f'版本号唯一来源 / 自动更新关键实现 OK（v{ver}）')


def test_install_dirty_update():
    """模拟远端有新版本 + 本地有未提交/未跟踪文件，确认自动更新且改动可找回。"""
    import shutil
    import subprocess

    if os.name != 'posix' or not shutil.which('bash') or not shutil.which('git'):
        print('跳过 install.sh 升级集成测试（需要 POSIX bash + git）')
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, 'tests', 'test_install_update.sh')
    result = subprocess.run(['bash', script], capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    print(result.stdout.strip())


def test_udp_controls_initial_state():
    """UDP 带宽输入框必须在首次渲染和定时任务弹窗打开时就同步禁用状态。"""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'app', 'templates', 'index.html'), encoding='utf-8') as f:
        html = f.read()
    assert 'function syncUdpControls(checkId, bandwidthId)' in html
    assert re.search(r'id="sUdpBw"[^>]*disabled', html), '定时任务的 UDP 带宽初始应禁用'
    init_block = html.split('async function init()', 1)[1][:800]
    assert "syncUdpControls('cfgUdp', 'cfgUdpBw')" in init_block
    assert "syncUdpControls('sUdp', 'sUdpBw')" in init_block
    seed_block = html.split('function seedScheduleForm(s)', 1)[1].split('function openScheduleForm', 1)[0]
    assert "syncUdpControls('sUdp', 'sUdpBw')" in seed_block
    print('UDP 带宽输入框初始状态同步 OK')


def test_brand_logo():
    """主页、登录页与 favicon 必须使用项目内 SVG 标识，不再使用 emoji。"""
    from app.app import app as flask_app
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ('index.html', 'login.html'):
        with open(os.path.join(root, 'app', 'templates', name), encoding='utf-8') as f:
            page = f.read()
        assert 'logo.svg' in page and '📡' not in page, name
        assert 'LinkPilot' in page, name
    with open(os.path.join(root, 'app', 'static', 'logo.svg'), encoding='utf-8') as f:
        logo = f.read()
    assert '<svg' in logo and '<title>LinkPilot logo</title>' in logo
    assert '#ff9f1c' in logo and '#2448a8' in logo
    resp = flask_app.test_client().get('/static/logo.svg')
    assert resp.status_code == 200
    assert resp.mimetype in ('image/svg+xml', 'image/svg+xml;charset=utf-8'), resp.mimetype
    print('SVG 品牌标识 / favicon OK')


if __name__ == '__main__':
    test_parse()
    test_units()
    test_evaluate()
    test_rating_system()
    test_report()
    test_xff_and_limiter()
    test_test_protocol()
    test_discovery_flow()
    test_probe_script()
    test_job_ids()
    test_probe_error_surfaced()
    test_test_params()
    test_multistream_and_udp_parse()
    test_report_params_and_udp_columns()
    test_machine_addr_override()
    test_server_lifecycle()
    test_daemon_guard()
    test_job_cleanup_paths()
    test_orphan_server_reclaim()
    test_schedule_params()
    test_schedule_lifetime_expiry()
    test_schedule_trigger_flow()
    test_schedule_restart_recovery()
    test_schedule_comparison_report()
    test_incomplete_direction_is_failure()
    test_active_job_keeps_machine_online()
    test_security_hardening()
    test_dashboard_interactions_present()
    test_event_stream_log_contract()
    test_run_list_returns_summary_only()
    test_version_sync()
    test_install_dirty_update()
    test_udp_controls_initial_state()
    test_brand_logo()
    print('\nALL TESTS PASSED')
