"""部署成功横幅：打印版本、面板地址、登录账号密码。"""
import json
import os
import re
import urllib.request

port = os.environ.get('PANEL_PORT', '8080')
data_dir = os.environ.get('DATA_DIR', '/data')


def app_version():
    """当前代码版本：以 app/app.py 的 APP_VERSION 为准（这里不再另外维护一份）。"""
    try:
        with open('/app/app/app.py', encoding='utf-8') as f:
            m = re.search(r"^APP_VERSION\s*=\s*'([^']+)'", f.read(), re.M)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ''


version = app_version()

ip = ''
try:
    ip = urllib.request.urlopen('https://api.ipify.org', timeout=3).read().decode().strip()
except Exception:
    pass

user, pw = '', ''
try:
    with open(os.path.join(data_dir, 'auth.json'), encoding='utf-8') as f:
        user = json.load(f).get('user') or 'admin'
except Exception:
    pass
try:
    with open(os.path.join(data_dir, '.initial_password'), encoding='utf-8') as f:
        pw = f.read().strip()
except Exception:
    pass

if not ip:
    ip = '<面板机IP>'

print(f"""
==================================================
 ✅  LinkPilot 面板已部署成功！{f'（v{version}）' if version else ''}
     面板地址:  http://{ip}:{port}
     登录账号:  {user or '（见 auth.json）'}
     登录密码:  {pw or '（首次部署时已显示；忘记可删除 auth.json 重启重新生成）'}
 下一步: 打开面板 → 添加机器 → 复制接入命令到机器上执行
==================================================""", flush=True)
