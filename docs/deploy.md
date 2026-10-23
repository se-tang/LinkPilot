# 部署与 HTTPS

> 一键部署命令见 [README「快速开始」](../README.md#快速开始)，这里只写它背后的细节。

## 部署脚本做了什么

脚本先自检运行环境，缺什么自动装什么，再拉代码启动面板：

1. 自检系统与包管理器（Debian/Ubuntu、CentOS/RHEL/Rocky/Alma/Fedora、Alpine、openSUSE、Arch）；
2. `curl`/`wget` + `ca-certificates` → `git` → **docker + docker compose v2**（官方安装脚本 → 发行版仓库 → compose 独立二进制逐级兜底，守护进程没起会自动拉起）；`git` 装不上时自动退回源码包下载；
3. 克隆到 `~/LinkPilot` → 生成 `.env`（随机端口）→ `docker compose up -d --build` → 等健康检查通过后打印部署横幅（含初始登录密码）。

要点：

- **安装输出是给人看的**：每一步一行（`▸ 步骤 ... 结果 (耗时)`），耗时的第三方命令（装 Docker、构建镜像）只显示 spinner + 已用秒数，它们的刷屏输出全部写进日志文件（默认 `/tmp/LinkPilot-install.log`，可用 `LINKPILOT_LOG` 改）。失败时自动回显日志末尾 6–10 行，不必再翻屏找原因；想看到全部原始输出就加 `LINKPILOT_VERBOSE=1`。管道 / CI 环境下自动退化为纯文本（无颜色、无 spinner 刷新）。
- **可重复执行**：再次运行就是升级（自动检查更新 → 有更新才重建），数据、端口、机器接入关系全部保留；之前用过 HTTPS 会自动带上 `--profile tls`，不会把 caddy 弄丢，并顺手清理被误挂载成目录的 `Caddyfile.local`。升级时不会重复显示初始密码（避免误导）。同一条命令在**没装过的机器上是安装、装过的机器上是升级**，详见下面的「升级」。
- **非 root 也能跑**：会通过 `sudo` 提权（需要机器上有 sudo）。
- 国内装 Docker 慢/超时：命令改成 `... | DOCKER_MIRROR=Aliyun bash`；机器走代理：命令最前面加 `https_proxy=http://IP:端口`。
- 环境变量：`LINKPILOT_DIR`（安装目录，默认 `~/LinkPilot`）、`LINKPILOT_REPO_URL`、`LINKPILOT_BRANCH`、`LINKPILOT_TLS=1`（同时启用 HTTPS 网关）、`LINKPILOT_VERBOSE=1`、`LINKPILOT_LOG`、`NO_COLOR=1`、`DOCKER_MIRROR`、`COMPOSE_MIRROR`、`LINKPILOT_TARBALL`。
- 脚本需要 `bash`（Alpine 等默认 ash 的系统先 `apk add bash`）；机器上连 `curl` 都没有时先 `apt-get update && apt-get install -y curl`（或 `yum install -y curl` / `apk add curl`）。
- 端口随机生成（记录在 `.env`，升级/重启不变），登录账号密码随机生成，均在部署横幅里显示一次；忘记密码删 `data/auth.json` 重启重新生成。
- 面板容器默认只读文件系统 + 最小权限运行。

想手动控制每一步：

```bash
git clone https://github.com/se-tang/LinkPilot.git && cd LinkPilot && ([ -f .env ] || echo "PANEL_PORT=$(shuf -i 10000-30000 -n 1)" > .env) && docker compose up -d --build && sleep 5 && docker compose logs --tail 15 linkpilot
```

## HTTPS（可选）

**方式 A：80/443 空闲** — Caddy 自动签发证书并反代（Caddy 本身就是反向代理，不需要再装 nginx / 宝塔）：

1. 域名 A 记录指向面板机公网 IP，并放行安全组/防火墙的 **80 与 443**（签发证书要求公网能访问 80）；
2. 写域名 + 开安全 Cookie，再拉起 Caddy：

```bash
cd ~/LinkPilot
echo "PANEL_DOMAIN=panel.example.com" >> .env    # ← 换成你自己的域名，必须设
echo "PANEL_COOKIE_SECURE=1" >> .env
cp -T Caddyfile.acme Caddyfile.local
docker compose --profile tls up -d
```

3. 确认证书签发与反代是否正常：

```bash
docker compose --profile tls logs --tail 30 caddy   # 出现 certificate obtained successfully 即成功
curl -I https://panel.example.com                   # 返回 200 / 302
```

`Caddyfile.acme` 里就是一句 `reverse_proxy linkpilot:8000`，容器名走 compose 内网，端口不用填。

> `PANEL_DOMAIN` 没设或为空时 Caddy 起不来（Caddyfile 的站点地址变成空，日志报 Caddyfile 解析错误）——补上域名重新 `docker compose --profile tls up -d` 即可。
> 80 端口被别的服务占用时 ACME HTTP-01 校验必然失败（拿不到证书），请腾出 80 或改用方式 B。
>
> ⚠️ **域名如果开着 Cloudflare 橙云**：Caddy 会把「CF 边缘 IP」当作对端，若把它写进 `X-Forwarded-For`，
> 面板就会把机器地址记成会不停变化的 CDN 地址（表现为机器列表里的 IP 每几秒变一次、测试报端口不可达）。
> 当前 `Caddyfile.acme` 已改为优先写 `CF-Connecting-IP`，面板侧也会自动纠偏；最稳妥仍是按 **方式 B** 配
> （Caddyfile.origin 直接监听 `:443` 并用 CF 源证书）。

**方式 B：80/443 被占用 + Cloudflare 橙云** — 用 Cloudflare 源证书（15 年有效，无需续期）。`Caddyfile.origin` 直接监听 `:443`，**不依赖 `PANEL_DOMAIN`**：

1. Cloudflare → SSL/TLS → **源服务器（Origin Server）** → 创建证书 → 把显示的证书和私钥分别存为面板机 `~/LinkPilot/certs/origin.pem`、`origin.key`
2. 切换 Caddy 配置并改端口（443 被占时用 8443）：

```bash
cd ~/LinkPilot
cp -T Caddyfile.origin Caddyfile.local
echo "CADDY_HTTPS=8443" >> .env
echo "PANEL_COOKIE_SECURE=1" >> .env
docker compose --profile tls up -d
```

3. Cloudflare **Rules → Origin Rules**（或 Rules → Overview → Create rule → Origin Rule）建一条规则：Hostname equals `panel.example.com` → Destination Port rewrite to `8443`
4. Cloudflare **SSL/TLS → 概述** 模式设为 **完全（严格）/ Full (strict)**

之后访问 `https://panel.example.com`。

> 若之前在未创建 `Caddyfile.local` 时启动过容器，Docker 会把它误建成一个**目录**（`ls -la` 显示为 `drwxr-xr-x`）。先 `rm -rf Caddyfile.local` 再重新执行上面的 `cp -T` 即可。

确认 HTTPS 正常后，在 `.env` 追加 `PANEL_BIND=127.0.0.1` 并 `docker compose --profile tls up -d`，关闭公网 HTTP 直连（面板与 Agent 均走域名接入）。

> ⚠️ 改成 `PANEL_BIND=127.0.0.1` 后，端口不再对外监听，**之前用 `http://IP:端口` 接入的 Agent 会全部失联**：回面板用 `https://你的域名` 重新生成接入命令，在每台机器上重跑一次即可（重复接入会用新地址重启服务，机器令牌不变）。还没切域名接入前，先别急着加 `PANEL_BIND`。

方式 B 下 Agent 真实 IP 取自 CF-Connecting-IP，**源站防火墙/安全组务必只放行 Cloudflare 回源网段**（https://www.cloudflare.com/ips/），否则能直连 Caddy 的人可自带该头伪造 agent_ip。

注意：登录限速按来源 IP 计，经反代部署时所有登录共享同一桶——他人 5 次失败会短暂锁住登录页（300 秒）。

## 升级

**重跑当初那条一键命令就行**——脚本会自己比较本地与远端：有更新就更新代码并重建镜像，没有就报告「已是最新」；数据、端口、机器接入关系全部保留，Agent 自动重连，登录密码不变。

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/LinkPilot/main/install.sh | bash
```

几个刻意的设计：

- **不怕远端历史被重写**：内部用 `git fetch` + 强制对齐分支，而不是 `git pull --ff-only`（后者遇到 force push 会直接失败，用户就会看到「有更新但升不动」）。
- **用新脚本继续本次部署**：走 `curl | bash` 时手里那份脚本是旧的，检测到更新后会用**更新后的** `install.sh` 重新执行一次，所以这次部署用的也是最新逻辑（只重跑一次）。
- **HTTPS 不会因容器被删而丢失**：升级时除了检查已有 Caddy 容器，也会读取 `.env` 中的 `PANEL_DOMAIN` / `PANEL_COOKIE_SECURE` / `CADDY_*` 和 `Caddyfile.local`，自动补上 `--profile tls`。
- **本地改动不会挡住升级**：检测到未提交代码时，先用 `git stash push --include-untracked` 自动备份，再把代码对齐到远端并继续部署；备份 ID 会显示在部署结论里。`.env`、`Caddyfile.local`、`certs/` 和 `data/` 属于运行时配置，升级检测会明确排除，不会进入 stash，也不会遮挡 HTTPS 配置。若入口是旧版脚本、配置已经被 stash，`v0.3.3` 会在生成新配置前自动找回这些运行时文件。备份失败时才会停止更新，避免静默覆盖。

只想检查/更新代码、先不部署：

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/LinkPilot/main/install.sh | LINKPILOT_DRY_RUN=1 bash
```

也可以照旧手动来：

```bash
cd ~/LinkPilot && git stash push -u -m "manual-upgrade-backup" \
  && git fetch origin main && git reset --hard origin/main \
  && docker compose --profile tls up -d --build
```

若升级时 caddy 报 `path does not exist`，说明之前的误挂载产生了同名目录，清一次即可：

```bash
rm -rf ~/LinkPilot/Caddyfile.local && cp -T ~/LinkPilot/Caddyfile.acme ~/LinkPilot/Caddyfile.local && docker compose --profile tls up -d
```

### 升级后 HTTPS 打不开（v0.3.1 及更早版本）

`v0.3.1` 会把未跟踪的 `.env` 当成普通本地改动存进 stash，可能顺带移除域名与 Caddy 端口配置，导致 IP 端口仍可访问但 HTTPS 反代失效。先找回升级前的配置：

```bash
cd ~/LinkPilot
git stash list
# 找到 “LinkPilot auto-update backup ...” 对应的一项，下面以 stash@{0} 为例
git ls-tree -r --name-only 'stash@{0}^3' | grep -E '^(\.env|Caddyfile\.local|certs/)'
cp -a .env .env.before-recovery 2>/dev/null || true
git restore --source='stash@{0}^3' -- .env Caddyfile.local certs
docker compose --profile tls up -d --force-recreate
```

如果 `.env` 不在 `stash@{0}`，按 `git stash list` 中的其它编号替换。`v0.3.2` 起 `.env`、`certs/` 会由 `.gitignore` 和 `.git/info/exclude` 双重保护；`v0.3.3` 还会在旧版脚本已经 stash 的情况下自动从升级备份恢复，因此后续正常升级**不需要**再手工修改 `PANEL_PORT` 或补回 `PANEL_BIND`。

## 卸载

面板机：

```bash
cd ~/LinkPilot && docker compose down && rm -rf data && rm -f .env && docker rmi linkpilot:latest
```

节点机：

```bash
curl -fsSL http://面板IP:端口/agent/uninstall.sh | bash
```

面板已删则在节点机直接执行：

```bash
systemctl disable --now linkpilot-agent 2>/dev/null; rm -f /etc/systemd/system/linkpilot-agent.service; systemctl daemon-reload 2>/dev/null; pkill -f "linkpilot/agent.sh" 2>/dev/null; rm -rf /usr/local/lib/linkpilot /etc/linkpilot /var/lib/linkpilot
```
