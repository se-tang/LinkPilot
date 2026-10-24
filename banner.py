"""Deployment banner. Never print passwords: container logs are retained."""
import json
import os
import re

data_dir = os.environ.get('DATA_DIR', '/data')


def app_version():
    """Read APP_VERSION from app/app.py, the single authoritative version source."""
    try:
        with open('/app/app/app.py', encoding='utf-8') as f:
            match = re.search(r"^APP_VERSION\s*=\s*'([^']+)'", f.read(), re.M)
            if match:
                return match.group(1)
    except OSError:
        pass
    return ''


version = app_version()
user = ''
try:
    with open(os.path.join(data_dir, 'auth.json'), encoding='utf-8') as f:
        user = json.load(f).get('user') or 'admin'
except Exception:
    pass

print(f"""
==================================================
 ✅  LinkPilot 面板已部署成功！{f'（v{version}）' if version else ''}
     登录账号:  {user or '（见 auth.json）'}
     登录密码:  （首次密码仅由安装脚本从本地 0600 文件读取并展示）
 下一步: 打开面板 → 添加机器 → 复制接入命令到机器上执行
==================================================""", flush=True)
