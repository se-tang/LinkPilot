<img src="app/static/logo.svg" width="104" height="59" alt="LinkPilot" align="left" hspace="12"/>

# LinkPilot

多机网络线路测试面板：一台目标机配合多台后端机执行 iperf3，记录上下行、延迟、丢包、抖动和重传，并持续观察长期趋势。

## 功能

- 手动多机测速，支持 TCP、UDP、IPv4、IPv6。
- 定时任务使用指定间隔和运行期限连续测试。
- 单线/多线吞吐趋势、跨轮汇总和逐轮明细。
- 测试记录按定时任务自动折叠，支持带图 Markdown 报告。
- Agent 接入，不在面板保存 SSH 凭据。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/LinkPilot/main/install.sh | bash
```

脚本会自动安装缺失的 Git、Docker 和 Docker Compose，然后生成随机端口与初始登录密码。

## 从 iperf3-fleet 升级

直接运行上面的同一条命令：

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/LinkPilot/main/install.sh | bash
```

脚本会自动识别旧的 `~/iperf3-fleet` 安装目录，在原目录切换为 LinkPilot 代码并重建容器。以下内容会保留：

- `.env`、端口和绑定地址
- 数据库、机器和测试记录
- Agent 接入关系
- HTTPS 域名、证书和 Caddy 数据卷

不要手工删除旧容器或直接修改容器名，否则可能造成同名冲突或丢失历史数据。

## 使用

1. 添加目标机与后端机。
2. 复制接入命令，在机器上以 root 执行。
3. 选择目标机、勾选后端机并开始测试。
4. 长期观察使用「定时任务」，完成后查看「汇总对比」。

## 文档

- [部署、HTTPS 与升级](docs/deploy.md)
- [参数与使用说明](docs/usage.md)
- [常见问题](docs/troubleshooting.md)
- [开发说明](docs/development.md)

## 兼容说明

为支持旧安装平滑升级，Docker 容器名、`IPERF3_FLEET_*` 环境变量和 Agent 服务名暂时保留 `iperf3-fleet` 标识；面板和项目名称已经统一为 LinkPilot。
