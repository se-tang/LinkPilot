#!/bin/bash
# 容器入口：启动面板 → 等待就绪 → 打印部署成功横幅 → 守护进程存活
cd /app

python -m app.app &
APP_PID=$!

for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break
  sleep 1
done

python /app/banner.py 2>/dev/null || true
# 初始密码只在首次部署展示一次，展示后移除明文
rm -f /data/.initial_password

while kill -0 "$APP_PID" 2>/dev/null; do
  sleep 5
done
