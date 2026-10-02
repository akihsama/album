# File Upload + Password Protected Subpage

这是一个基于 Flask 的最小示例网站：

- 支持上传文件（限制常见扩展名）
- 访问受保护页面前需要输入密码
- 受保护页面中可查看上传文件列表并下载

## 快速启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PASSWORD="your-password"
export SECRET_KEY="generate-a-random-secret"
mkdir -p uploads
python app.py
```

然后访问：

- http://127.0.0.1:5000/
- http://127.0.0.1:5000/protected

受保护页面会跳转到登录页，输入 `PASSWORD` 后才能访问。

## 生产环境注意事项

- 不要将密码直接写死在代码里；建议通过环境变量、配置管理或密钥管理服务注入
- 生产环境应启用 HTTPS
- 需要限制上传文件类型和体积
- 需要对上传文件做权限和安全控制
