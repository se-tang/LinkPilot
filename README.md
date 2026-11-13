# LinkPilot

多机网络线路测试面板。支持手动测速、定时任务、趋势对比和 Markdown 报告。

## 安装

默认安装后直接打印可访问地址：

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/LinkPilot/main/install.sh | bash
```

## HTTPS 反向代理

先准备 HTTPS 反代，安装时传入最终公网地址即可。安装器会按反代模式配置 Agent、Secure Cookie 和访问地址，不再要求 SSH 隧道或直连 NAT：

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/LinkPilot/main/install.sh \
  | PANEL_PORT=28874 \
    PANEL_PUBLIC_URL=https://panel.example.com \
    bash
```

反向代理上游：

```text
http://面板机可达地址:28874
```

同机反代时加 `PANEL_BIND=127.0.0.1`；远程反代保持默认 `0.0.0.0`。安装完成页会直接打印反代上游地址。Agent 接入地址自动使用 `https://panel.example.com`。

## 升级

用安装时的同一条命令重跑即可，端口、数据、密码和 Agent 关系保持不变。

详细配置见 [部署文档](docs/deploy.md)，参数见 [使用说明](docs/usage.md)。
