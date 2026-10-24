#!/bin/bash
# linkpilot agent 一键卸载脚本（由面板下发，也可单独使用）
systemctl disable --now linkpilot-agent 2>/dev/null
systemctl disable --now linkpilot-agent-uninstall.path 2>/dev/null
rm -f /etc/systemd/system/linkpilot-agent.service \
  /etc/systemd/system/linkpilot-agent-uninstall.service \
  /etc/systemd/system/linkpilot-agent-uninstall.path
systemctl daemon-reload 2>/dev/null
pkill -f "linkpilot/agent.sh" 2>/dev/null
# 顺手收掉面板起的 iperf3 server：正常收尾靠面板下发关闭任务，但删机器时面板往往
# 已经联系不上这台机器（或记录被直接删掉），这里本地兜底。pid 文件只会由面板的
# 启动脚本创建，所以按 pid 文件清理不会动到与本面板无关的 iperf3 进程。
for f in /tmp/iperf3-server*.pid; do
  [ -e "$f" ] || continue
  kill "$(cat "$f" 2>/dev/null)" 2>/dev/null
  rm -f "$f"
done
crontab -u linkpilot -l 2>/dev/null | grep -v "linkpilot/agent.sh" | crontab -u linkpilot - 2>/dev/null
rm -rf /usr/local/lib/linkpilot /etc/linkpilot /var/lib/linkpilot
userdel linkpilot 2>/dev/null || true
echo "✅ LinkPilot agent 已卸载（服务、自启、文件、残留 iperf3 server 均已清理）"
