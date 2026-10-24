# 开发

## 目录结构

```
app/
  app.py              面板 Web 服务：登录、机器管理、Agent API、任务接口、定时任务接口
  db.py               SQLite 数据层：机器、任务队列、测试记录、定时任务、长期对比数据
  runner.py           测试编排：环境准备 → iperf3 串行 → ping 并行 → 报告
  scheduler.py        定时任务调度：排期 / 触发 / 冲突处理 / 重启恢复 / 跨轮对比报告
  agent_jobs.py       下发给 Agent 执行的脚本（装 iperf3、起 server、地址探测）
  agent/              节点机接入 / 卸载脚本（install.sh 由面板下发给节点）
  templates/          前端单页 index.html
tests/test_sample.py  解析 / 评价 / 报告 / 协议 / 地址探测 / 任务编号 / 定时任务 测试
install.sh            面板一键部署脚本
Caddyfile.acme        自动签证书反代模板（方式 A）
Caddyfile.origin      Cloudflare 源证书反代模板（方式 B）
docker-compose.yml    面板（+ 可选 caddy profile）
```

## 跑测试

```bash
python tests/test_sample.py     # 直接运行全部用例
```

纯标准库，不需要装依赖；涉及 shell 的用例（`SCRIPT_REPORT_IPS` 地址提取）只在 POSIX（Linux / macOS / 容器）下执行，Windows 上自动跳过。

## 在 WSL 里开发 / 验证（推荐）

面板本身是 Docker + bash 的活儿，在 Windows 上用 PowerShell 拼这类命令（路径、引号、编码）极易踩坑，本仓库的验证都放在 WSL 里做：

```powershell
# 在 WSL 里执行一条命令（示例：跑测试）
wsl -d Ubuntu -- bash -lc "cd /mnt/d/deepseek工作区/LinkPilot && python3 tests/test_sample.py"
```

工作区在 WSL 里就是 `/mnt/d/deepseek工作区/...`；反过来 Windows 也能用 `\\wsl$\Ubuntu\home\deepseek` 访问 WSL 内部文件。

两个辅助脚本（首次搭环境用，可重复执行）：

| 文件 | 作用 |
|:--|:--|
| [wsl-install-windows.ps1](wsl-install-windows.ps1) | 在 Windows 装 WSL2 + Ubuntu。**关键坑**：Windows 功能只提供内核组件，真正可用的 `wsl.exe` 来自 Store/MSI 应用包；旧的内置壳上 `wsl --install` 会一直报「未安装」并让你再跑一次它自己。所以这里直接下官方 x64 MSI 静默安装（`winget install Microsoft.WSL` 也行，但它会先调旧壳的 `--update` 而失败）。 |
| [wsl-setup.sh](wsl-setup.sh) | 在 Ubuntu 里配置环境：sudo 免密、apt 换阿里云镜像、常用工具、systemd、Docker + **国内镜像加速**。 |

```powershell
# 重启后，以 root 在 WSL 里配置环境
wsl -d Ubuntu -u root -- bash /mnt/d/deepseek工作区/LinkPilot/docs/wsl-setup.sh
```

> 国内网络两条硬约束（脚本里已处理，手工搭时别漏）：
> 1. **apt** 必须换镜像（官方源基本不动）；
> 2. **Docker Hub 直连不通**（`registry-1.docker.io` 超时），必须配 `registry-mirrors`，否则拉 `python:3.12-slim` / `caddy` 会失败。实测可用的加速站：`docker.m.daocloud.io`、`docker.1ms.run`、`docker.1panel.live`。
>
> 另外：Windows 上开着 `127.0.0.1:10808` 这类代理时，WSL 出站**用不到**它（NAT 模式），所以 WSL 内按直连规划；需要时可在 `%USERPROFILE%\.wslconfig` 写 `[wsl2]` + `networkingMode=mirrored` 让 WSL 共享 Windows 网络（含 localhost 代理）。

## 提交前

```bash
git config core.hooksPath hooks
```

启用防呆钩子：拦截 `data*/`、`auth.json`、`secret_key`、`panel.db*` 及疑似密钥内容的提交（确认为误报时 `--no-verify` 可跳过）。

## 改动的生效范围

| 改什么 | 怎么生效 |
|:--|:--|
| 面板（`app.py` / `db.py` / `runner.py` / `agent_jobs.py` / 模板） | 面板机 `git pull` 后重建镜像（见 [deploy.md](deploy.md#升级)），节点机无需任何操作 |
| `app/agent/install.sh`（Agent 本体） | 必须到节点机重跑一次接入命令；当前只接受 v2 结构化任务协议 |

`app/agent/install.sh` 会下发到所有节点，**重复执行必须幂等**；下发给 Agent 的任务脚本（`agent_jobs.py`）会随心跳生效，不用重装 Agent。
