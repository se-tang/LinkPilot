# LinkPilot

<img src="docs/assets/linkpilot-logo.png" width="520" alt="LinkPilot Network Lab"/>

多机网络线路测试面板：一台目标机配合多台后端机执行 iperf3，记录上下行、延迟、丢包、抖动和重传，并持续观察长期趋势。

## 功能

- 手动多机测速，支持 TCP、UDP、IPv4、IPv6。
- 定时任务使用指定间隔和运行期限连续测试。
- 单线/多线吞吐趋势、跨轮汇总和逐轮明细。
- 测试记录按定时任务自动折叠，支持带图 Markdown 报告。
- 线路质量使用 S/A/B/C/F 五级评级，提供异常标签、诊断详情和测试摘要。
- 实时日志按预检、Ping、上行、下行、分析、完成阶段汇总，原始日志仍可展开查看。
- Agent 接入，不在面板保存 SSH 凭据。
- 面板可直接把 Agent 更新推送到节点（签名任务 + root 验签替换），不用逐台上机重跑命令。
- iperf3 走 `-J` 结构化输出：报告给出区间稳定性（CV / P95），TCP 默认剔除慢启动。
- SSE 实时刷新；单轮/长期数据可导出 CSV、JSON。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/LinkPilot/main/install.sh | bash
```

脚本会自动安装缺失的 Git、Docker 和 Docker Compose，生成独立的管理端口、Agent 端口与初始登录密码。

- 管理面默认直接监听公网端口，安装完成后会打印可访问的公网 IP 地址和端口。
- 需要 SSH 隧道或本机反向代理时，可显式设置 `PANEL_BIND=127.0.0.1`。
- Agent 使用独立公网端口，且只开放注册、心跳和结果回传接口。
- 默认直连模式允许 Agent 使用 HTTP；公网长期使用时建议限制安全组来源或再配置 HTTPS。

## 使用

1. 添加目标机与后端机。
2. 复制一次性接入命令，在机器上以 root 执行；安装阶段完成后 Agent 以低权限用户运行。
3. 选择目标机、勾选后端机并开始测试。
4. 长期观察使用「定时任务」，完成后查看「汇总对比」。

## 文档

- [部署、HTTPS 与升级](docs/deploy.md)
- [参数与使用说明](docs/usage.md)
- [常见问题](docs/troubleshooting.md)
- [开发说明](docs/development.md)
- [更新记录](docs/changelog.md)
