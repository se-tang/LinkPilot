# LinkPilot

多机网络线路测试面板。支持手动测速、定时任务、趋势对比和 Markdown 报告。

## 安装

安装后直接打印公网 HTTP 地址：

```bash
curl -fsSL https://raw.githubusercontent.com/se-tang/LinkPilot/main/install.sh | bash
```

## HTTPS 反代

面板不需要额外配置。任意反代把请求转发到面板 HTTP 端口即可：

```nginx
location / {
    proxy_pass http://面板机IP:管理端口;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

之后通过 `https://你的域名` 打开面板。生成的目标机接入命令自动使用浏览器当前域名，不需要配置 `PANEL_AGENT_URL`。

## 升级

重跑安装命令即可，端口、数据、密码和 Agent 关系保持不变。

详细配置见 [部署文档](docs/deploy.md)，参数见 [使用说明](docs/usage.md)。
