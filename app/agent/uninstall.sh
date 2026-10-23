#!/bin/bash
# iperf3-fleet agent 一键卸载脚本（由面板下发，也可单独使用）
systemctl disable --now iperf3-fleet-agent 2>/dev/null
rm -f /etc/systemd/system/iperf3-fleet-agent.service
systemctl daemon-reload 2>/dev/null
pkill -f "iperf3-fleet/agent.sh" 2>/dev/null
# 顺手收掉面板起的 iperf3 server：正常收尾靠面板下发关闭任务，但删机器时面板往往
# 已经联系不上这台机器（或记录被直接删掉），这里本地兜底。pid 文件只会由面板的
# 启动脚本创建，所以按 pid 文件清理不会动到与本面板无关的 iperf3 进程。
for f in /tmp/iperf3-server*.pid; do
  [ -e "$f" ] || continue
  kill "$(cat "$f" 2>/dev/null)" 2>/dev/null
  rm -f "$f"
done
crontab -l 2>/dev/null | grep -v "iperf3-fleet/agent.sh" | crontab - 2>/dev/null
rm -rf /usr/local/lib/iperf3-fleet /etc/iperf3-fleet /var/lib/iperf3-fleet
echo "✅ LinkPilot agent 已卸载（服务、自启、文件、残留 iperf3 server 均已清理）"
