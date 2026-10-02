# File Upload + Password Protected Subpage (Enhanced)

此版本在之前基础上增加：

- 支持多文件上传
- 支持文件删除（需登录）
- 支持图片预览（在受保护页面内显示缩略图，点击可查看原图）
- CSRF 保护的简单实现
- 会话 Cookie 安全配置（在 TLS 下生效）
- 可用 `ENABLE_SSL=1` 启用 Flask 的 adhoc TLS（仅用于测试）以避免明文传输

## 快速启动（开发）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PASSWORD="your-password"
export SECRET_KEY="generate-a-random-secret"
mkdir -p uploads
# 可选：启用临时 TLS（仅测试）
export ENABLE_SSL=1
python app.py
```

在浏览器访问：
- https://127.0.0.1:5000/  （如果启用了 ENABLE_SSL）
- http://127.0.0.1:5000/   （否则）

登录后访问 /protected 可查看、预览与删除上传的文件。

## 部署建议（生产）

- 始终在前端使用 TLS（HTTPS）。不要在生产中直接使用 Flask 的内置服务器暴露到互联网。使用 Nginx/Traefik/Caddy 或云负载均衡来终止 TLS 并反向代理到 Gunicorn/Uvicorn。
- 在部署环境中确保 `SESSION_COOKIE_SECURE` 为 true（本仓库在 app.config 中已设置）并通过环境变量传入强 `SECRET_KEY` 与 `PASSWORD`。
- 对上传文件做额外检查（检查 MIME、扫描病毒、限制最大分辨率/尺寸等）。
- 如果多用户访问，使用真实用户认证（Flask-Login + 数据库）、权限与审计日志。

