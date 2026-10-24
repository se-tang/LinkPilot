"""Legacy numeric helpers shared by runner; no shell is generated or dispatched here."""
import ipaddress
import re


SERVER_TTL_DEFAULT = 1800
SERVER_TTL_MIN = 300
SERVER_TTL_MAX = 3600


def valid_port(port, default=5201):
    try:
        value = int(str(port).strip())
    except (TypeError, ValueError):
        return default
    return value if 1 <= value <= 65535 else default


def clamp_ttl(seconds, default=SERVER_TTL_DEFAULT):
    try:
        value = int(float(seconds))
    except (TypeError, ValueError):
        return default
    return max(SERVER_TTL_MIN, min(SERVER_TTL_MAX, value))


def parse_ip_report(out):
    """Parse the Agent's local-address report and keep global addresses only."""
    found = {}
    for line in (out or '').splitlines():
        match = re.match(r'^(IPV4|IPV6)=(.*)$', line.strip())
        if not match:
            continue
        key = 'ip4' if match.group(1) == 'IPV4' else 'ip6'
        try:
            addr = ipaddress.ip_address(match.group(2).strip())
        except ValueError:
            continue
        if addr.version != (4 if key == 'ip4' else 6) or not addr.is_global:
            continue
        found[key] = addr.compressed
    return found
