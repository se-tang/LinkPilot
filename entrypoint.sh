#!/bin/bash
# 容器入口：启动面板 → 等待就绪 → 打印部署成功横幅 → 守护进程存活
set -u
cd /app

python -m app.app &
APP_PID=$!

# 容器收到 docker stop / compose down 的 SIGTERM 时，把它转给面板进程，
# 让调度线程和 SQLite 正常收尾（否则 10 秒后会被 SIGKILL 直接打断）。
forward_term() {
  kill -TERM "$APP_PID" 2>/dev/null || true
}
trap forward_term TERM INT

for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break
  sleep 1
done

python /app/banner.py 2>/dev/null || true

# 等面板退出；被信号打断时 wait 会提前返回，再等一次让进程真正收尾。
while true; do
  wait "$APP_PID"
  rc=$?
  kill -0 "$APP_PID" 2>/dev/null || break
done
exit "$rc"
