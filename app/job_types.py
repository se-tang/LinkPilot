"""Structured Agent job protocol.

The panel stores a job type plus validated scalar parameters. The Agent accepts
only these types and constructs the corresponding local command itself; no
free-form shell text is signed, transported, or executed.
"""
import ipaddress
import re


_BANDWIDTH_RE = re.compile(r'^\d{1,5}(?:\.\d{1,3})?[KMG]$')

KINDS = {
    'ensure_tools': set(),
    'report_ips': set(),
    'check_daemon': {'ports'},
    'start_server': {'port', 'ttl', 'bind'},
    'stop_server': {'ports'},
    'check_port': {'host', 'port'},
    'iperf_client': {
        'host', 'port', 'duration', 'streams', 'udp', 'udp_bandwidth', 'reverse',
    },
    'ping': {'host', 'count'},
    'uninstall': set(),
}


def _as_int(value, lo, hi, label):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f'{label}必须是整数')
    if not lo <= number <= hi:
        raise ValueError(f'{label}需在 {lo}-{hi} 之间')
    return number


def _as_bool(value, label):
    if value in (True, 1, '1', 'true', 'True'):
        return True
    if value in (False, 0, '0', 'false', 'False', None, ''):
        return False
    raise ValueError(f'{label}必须是布尔值')


def _as_ip(value, label='host'):
    try:
        return ipaddress.ip_address(str(value or '').strip()).compressed
    except ValueError:
        raise ValueError(f'{label}必须是合法 IP 地址')


def _as_ports(value):
    if isinstance(value, str):
        value = [part for part in value.replace(',', ' ').split() if part]
    if not isinstance(value, (list, tuple, set)):
        raise ValueError('ports 必须是端口列表')
    ports = sorted({_as_int(port, 1, 65535, '端口') for port in value})
    if len(ports) > 32:
        raise ValueError('ports 最多允许 32 个端口')
    return ports


def normalize(kind, params=None):
    """Validate and return a fresh parameter dict containing only allowed keys."""
    if kind not in KINDS:
        raise ValueError(f'不支持的任务类型：{kind}')
    params = dict(params or {})
    allowed = KINDS[kind]
    extra = set(params) - allowed
    if extra:
        raise ValueError(f'任务参数包含未知字段：{", ".join(sorted(extra))}')

    if kind in ('ensure_tools', 'report_ips', 'uninstall'):
        return {}
    if kind in ('check_daemon', 'stop_server'):
        return {'ports': _as_ports(params.get('ports'))}
    if kind == 'start_server':
        return {
            'port': _as_int(params.get('port'), 1, 65535, '端口'),
            'ttl': _as_int(params.get('ttl'), 300, 3600, 'TTL'),
            'bind': _as_ip(params.get('bind'), '绑定地址'),
        }
    if kind == 'check_port':
        return {
            'host': _as_ip(params.get('host')),
            'port': _as_int(params.get('port'), 1, 65535, '端口'),
        }
    if kind == 'iperf_client':
        bandwidth = str(params.get('udp_bandwidth') or '100M').strip().upper()
        if not _BANDWIDTH_RE.match(bandwidth):
            raise ValueError('UDP 带宽必须带 K/M/G 单位')
        return {
            'host': _as_ip(params.get('host')),
            'port': _as_int(params.get('port'), 1, 65535, '端口'),
            'duration': _as_int(params.get('duration'), 1, 300, '时长'),
            'streams': _as_int(params.get('streams'), 1, 32, '线程数'),
            'udp': _as_bool(params.get('udp'), 'UDP'),
            'udp_bandwidth': bandwidth,
            'reverse': _as_bool(params.get('reverse'), 'reverse'),
        }
    if kind == 'ping':
        return {
            'host': _as_ip(params.get('host')),
            'count': _as_int(params.get('count'), 1, 2000, 'ping 次数'),
        }
    raise AssertionError(kind)


def encode(kind, params=None):
    """Canonical line protocol signed by the panel and parsed by the Agent."""
    clean = normalize(kind, params)
    lines = [f'kind={kind}']
    for key in sorted(clean):
        value = clean[key]
        if isinstance(value, bool):
            value = '1' if value else '0'
        elif isinstance(value, list):
            value = ','.join(str(item) for item in value)
        lines.append(f'{key}={value}')
    return '\n'.join(lines) + '\n'


def preview(kind, params=None):
    """A local command preview for logs; never sent to the Agent."""
    clean = normalize(kind, params)
    if kind == 'ensure_tools':
        return 'ensure iperf3 / ping'
    if kind == 'report_ips':
        return 'report local IPv4 / IPv6'
    if kind == 'check_daemon':
        return 'check iperf3 daemon / ports ' + ','.join(map(str, clean['ports']))
    if kind == 'start_server':
        return (f'iperf3 -s -B {clean["bind"]} -p {clean["port"]} '
                f'(ttl {clean["ttl"]}s)')
    if kind == 'stop_server':
        return 'stop iperf3 servers on ' + ','.join(map(str, clean['ports']))
    if kind == 'check_port':
        return f'connect {clean["host"]}:{clean["port"]}'
    if kind == 'iperf_client':
        flags = ''
        if clean['udp']:
            flags += f' -u -b {clean["udp_bandwidth"]}'
        if clean['reverse']:
            flags += ' -R'
        return (f'iperf3 -c {clean["host"]} -p {clean["port"]} '
                f'-t {clean["duration"]} -P {clean["streams"]}{flags}')
    if kind == 'ping':
        return f'ping -c {clean["count"]} -i 1 {clean["host"]}'
    if kind == 'uninstall':
        return 'uninstall LinkPilot Agent'
    return kind
