# 排障

先看两处日志：

```bash
# 面板侧
docker compose --profile tls logs --tail 100 linkpilot
# 节点机侧
tail -20 /var/lib/linkpilot/agent.log
```

| 现象 | 原因 / 处理 |
|:--|:--|
| 面板里机器地址**每几秒变一次**、而且不是机器真实 IP | 面板前面有 CDN（多为 Cloudflare 橙云），反代把「CDN 边缘 IP」当成了对端写进 `X-Forwarded-For`，面板就把它记成了机器地址。当前版本已能自动改用 `CF-Connecting-IP`；应急也可以在机器资料里填「手动测试地址」。Caddyfile.acme 模板已改为优先取 `CF-Connecting-IP`（`cp -T Caddyfile.acme Caddyfile.local && docker compose --profile tls up -d` 生效）。 |
| 测试报「目标机 x.x.x.x 的 NNNN 端口从本机不可达」 | ①先看面板显示的**测试地址**是不是目标机真实 IP（上一条若地址是 CDN 地址，必然不可达）；②在目标机上 `ss -ltn \| grep NNNN` 确认 server 起没起（面板日志里有 `[目标] 启动 iperf3 server：监听端口 …`）；③查商家安全组 / NAT 是否放行并映射了该端口。 |
| 面板日志告警「检测到发行版自带的 iperf3 常驻服务」，或测试报「目标机 iperf3 -s 启动失败（端口 5201）」且日志里有 `Address already in use` | 这台机器上有人**手工**装 iperf3 时选了「作为守护进程启动」，于是有个 systemd 服务一直占着 5201（命令行 `iperf3 --server`、`Restart=always`，面板既杀不掉也抢不到端口，还会被它拖累成启动失败）。处理：目标机执行 `systemctl disable --now iperf3` 后重试；或把面板里的「目标机端口」改成别的端口（如 5202）绕开。确认是谁占的：`ss -ltnp \| grep 5201`。用面板自动安装的机器不会出现这个问题（非交互安装取的是默认值「否」）。 |
| 测试已经结束，目标机上 `ss -ltnp \| grep iperf3` 还能看到 `iperf3 -s` 在监听 | 正常情况下测试结束（含手动停止、出错中断）面板会立即关闭 server。仍能看到的多半是：面板容器在测试中被重启 / 目标机 Agent 掉线，导致关闭任务送不到。这类残留面板会在**重启后自动回收**（它按测试记录里存的「本轮起过的端口」精确清理，日志里会打印 `[回收] 已清理机器「X」上残留的 iperf3 server（端口 …）`），即使目标机当时离线，等它上线后也会补做；同时进程本身还有**存活上限**兜底（面板日志里写了 `server 存活上限 N 秒`，300–3600 秒）到点自动退出。要立刻收掉：`for f in /tmp/iperf3-server*.pid; do kill "$(cat "$f")"; rm -f "$f"; done`，或直接 `pkill -f "iperf3 -s -p NNNN"`（**别用不带 -p 的裸 `pkill -f 'iperf3 -s'`**，会杀掉机器上与本面板无关的 iperf3）。根治手段仍是安全组只放行后端机 IP，见 [usage.md](usage.md#端口与安全组)。 |
| 机器显示**在线**，但测试地址旁有 ⚠️、或一直显示「无 IPv6」 | 面板下发的地址探测任务没被 Agent 执行完。机器列表的 ⚠️ 鼠标悬停会写明原因；面板日志 `grep 探测` 可见 `[探测] 机器 #N 地址: {...}`（成功）或 `未获取到全局地址`。 |
| 机器上 `agent.log` 出现「**拒绝执行：任务 #N 为重放**」 | Agent 本地记录的任务编号水位比面板当前编号大：面板数据库被重置（重新部署 / 删过 `data`）后任务编号从 1 重新开始，Agent 会把之后所有任务当重放**静默拒绝**。当前版本的面板任务编号改为时间戳打底，**升级即自愈，无需重装任何 Agent**；重新执行一次接入命令也会重置水位。 |
| 测试任务全部「任务执行超时」，机器上却没有日志 | 同上，Agent 拒收了任务。 |
| 机器上 `ip -6 addr show scope global` 只有 `fe80::` 或 `fc00::` / `fd00::` | 这台机器确实没有**全局可路由**的 IPv6（链路本地 / ULA 不能用于跨公网测试），面板显示「无 IPv6」是正确结果，不是探测故障。 |
| 机器在线但地址一直停在「探测中…」 | 心跳正常只说明连通；地址要靠探测任务补齐，点「探测地址」手动重试即可。 |
| Agent 一直不上线 | 机器上 `systemctl status linkpilot-agent` 与 `agent.log`：`curl_exit=60` 多为证书问题（用 IP 接入却配了 https 域名），`curl_exit=28` 是连不上面板（防火墙 / 安全组 / DNS），`curl_exit=22` 是令牌被面板拒绝（机器被删过，需重新接入）。 |
| 面板打不开 | `docker compose ps` 看容器状态与健康检查；`docker compose logs --tail 50 linkpilot`。 |
| Caddy 证书签不下来 | 域名 A 记录是否指向本机、安全组是否放行 80/443、80 是否被别的服务占用；`docker compose --profile tls logs --tail 30 caddy`。 |
| 面板容器反复重启 | 常见是端口冲突（改 `.env` 的 `PANEL_PORT` 后 `docker compose up -d`）或磁盘空间不足。 |
| 定时任务的「最近一轮」显示**本轮跳过 / 等待空档** | 触发时面板里已有测试在跑（手动或别的定时任务）。默认策略是跳过本轮并写下原因（不排队，避免长期对比的时间轴被推后）；勾上任务的「与手动测试冲突时等待」就会等空档再跑。 |
| 定时任务一直**没跑起来**，最近一轮写着「机器已被删除 / 角色已不是…」 | 任务里选的目标机或后端机被删了，或角色被改成别的（例如把目标机改成了后端机）。到任务里编辑重新选机器即可；这类问题只跳过该轮，不会影响后面的排期。 |
| 定时任务的「下一轮」显示**已中断** | 面板（容器）在那轮测试进行中被重启。排期不受影响，下一轮会按原计划继续；那轮的测试记录在「测试记录」里状态为失败并写明「面板服务重启导致测试中断」。 |
| 想确认定时任务的参数或排期 | 面板列表里每行都写了参数与间隔；也可以直接查库（**务必只读**，见下一条）：`docker compose exec linkpilot python -c "import sqlite3;c=sqlite3.connect('file:/data/panel.db?mode=ro',uri=True);print(list(c.execute('select id,name,next_run_at,last_status,run_count,fail_count from schedules')))"`。 |
| 手工进去查库/调试时，**正在跑的测试或任务突然被判成「面板重启导致中断」** | 查库脚本里调用了 `db.init_db()` 或 `db.mark_stale_runs()`。`init_db()` 里带着「启动自愈」逻辑：它会把所有 running/pending 的测试标成失败、把排队任务写上一句「面板重启，任务作废」——**这是面板启动时该做的事，运行中调用等于自己制造故障**。诊断请用上面的只读连接（`mode=ro`），不要 import 面板模块后调 `init_db()`。 |
| 安装脚本输出太少，想看到被收起的原始输出 | 加 `LINKPILOT_VERBOSE=1` 重跑；或直接看日志文件（默认 `/tmp/LinkPilot-install.log`，`LINKPILOT_LOG` 可改）。 |
