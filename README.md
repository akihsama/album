# album — 口令登录的私人云盘

单用户私有相册：上传 / 预览 / 下载 / 删除，图片自动生成缩略图。

## 现在有什么

- **打开根地址就是这个门**：未登录只有密码框，输完直接进相册（7 天内不用再输）；
  上传页在 `/upload`
- 口令用 **Argon2id** 校验（不再明文比较），登录限流 20/分钟，会话 7 天；
  限流命中给的是「XX 秒后自动重试」页面，不是裸 429
- 上传自动生成 **480px 缩略图**（默认 WebP，实测 55KB/张），列表只加载缩略图
- **中文文件名完整保留** —— 磁盘用 UUID 存，真实名进 SQLite
  （改之前 `secure_filename` 会把 `旅行 相册(1).png` 变成 `1.png`）
- **内容去重**：落盘时算 SHA-256，同一张图传两次只占一份空间（秒传）
- **游标分页** + 无限滚动 + 原生懒加载，几千张也能流畅滚
- 灯箱预览：键盘 ←/→ 翻页，ESC 关闭；下载保留原始中文名
- 路由只接受 32 位十六进制 id，路径穿越在进文件系统之前就被挡掉

## 在多台终端上使用（手机 / 平板 / 另一台电脑）

每个终端各输一次密码就行，之后 7 天内不用再输 —— 会话是存在各自浏览器里的，
一台设备登出不会踢掉另一台（有回归用例守着：
`MultiDeviceTest.test_second_device_is_independent_of_first`）。

按「你在不在同一个 WiFi 下」选一条路，两条**互不干涉、各自都能单独跑通**，
默认端口也不同（局域网 5000 / 公网 5001），甚至可以同时开着：

### ① 在同一个 WiFi 下 → 局域网（最快）

```bash
python scripts/serve_lan.py        # 打印可点击的局域网地址，手机/另一台电脑直接开
```

`serve_lan.py` 用 `HOST=0.0.0.0` 拉起服务并打印地址，`Ctrl+C` 收工。
- 端口被占会直接告诉你换哪个，不硬顶。
- Windows 首次会弹防火墙提示：**允许专用网络**（不要勾公用网络）。

### ② 不在同一个 WiFi（出门/异地） → 公网（加密）

```bash
python scripts/serve_public.py                    # 一条命令拿到 https 地址
python scripts/serve_public.py --password 你的口令  # 顺便把口令设好
```

`serve_public.py` 走 **Cloudflare Tunnel**：免费、免注册、不用买域名、不用开端口映射，
也不暴露你家 IP，并且自动签 HTTPS 证书 —— 这是硬要求：`BEHIND_TLS=1` 时会话 cookie
带 `Secure`，只有 https 才发得出去，裸 http 上公网等于把密码明文甩在带宽里。
- 它只把相册绑在 `127.0.0.1`，公网入口由隧道提供，本机端口不对互联网开放；
- 首次会下载 `cloudflared` 到 `tools/`（约 50MB），之后复用；
- Ctrl+C 只收自己拉起的那两个进程，不会误伤局域网那条路的服务。

#### 固定公网地址（不想每次重启都变）

免费 quick tunnel 每次重启地址都会变（`*.trycloudflare.com` 是随机的）。要「永远是同一个
网址」得用 **Cloudflare 命名隧道 + 你自己的域名**，这一步只做一次，交给
`scripts/serve_fixed.py`。两种方式任选其一：

**A. 不想碰 token（推荐）** —— `cloudflared` 自己用浏览器登录你的 Cloudflare 账号：

```bash
python scripts/serve_fixed.py --init
```

1. 脚本自己弹浏览器让你登录、授权域名（只做一次，凭据存 `~/.cloudflared/cert.pem`）；
2. 自动检查账号里有没有叫 `album` 的隧道，有就复用、没有才建（不会留一堆孤儿隧道）；
3. 把**隧道 ID + 凭据文件路径**写进 `.env` —— 这条路**根本不产生 token**，
   Cloudflare 后台那个「只显示一次」的窗口错过也不怕；
4. 告诉你后台还差哪一步（加一条 CNAME / Public hostname），配好 DNS 就好。

**B. 手动从后台复制 token**：

```bash
python scripts/serve_fixed.py --setup --host album.你的域名.com
```

> token 在后台的位置（忘了只能在这儿找回来）：
> [dash.cloudflare.com](https://dash.cloudflare.com/) → **Networking → Tunnels**
> → 点你的隧道 → **Add a replica**（或 Edit）→ 复制那条 `cloudflared tunnel run --token **eyJ...**`
> 安装命令里 `eyJ` 开头的一长串。**创建时只显示一次，错过就只能从这里再取。**

它干什么：
1. （方式 A 已代劳）拿到隧道凭据：token 方式让你粘过来（**不回显**，只写进 `.env`）；
2. 在 `tools/tunnel-album.yml` 里生成一份 cloudflared 配置（域名 → `127.0.0.1:5001`，
   最后带 `http_status:404` 兜底，缺这条 cloudflared 拒绝启动）；
3. 查一次 DNS：这个域名在 CNAME 里有没有指向 `<隧道ID>.cfargotunnel.com`。
   没指过去会直接告诉你「后台还没配好」去哪个菜单配，而不是让你对着打不开的地址猜。

之后每次启动就是一行：

```bash
python scripts/serve_fixed.py                      # https://album.你的域名.com
python scripts/serve_fixed.py --check              # 只体检（配置/DNS），不起服务
python scripts/serve_fixed.py --port 6001          # 端口换一个
```

- 三条路默认端口分开（局域网 5000 / quick 公网本机侧 5001 / 固定域名 5001），
  并且同样只绑 `127.0.0.1`，公网入口只有隧道那一个。
- **token 只走环境变量**，不出现在任何命令行里（命令行会被任务管理器、`ps`、
  shell history 看见，等于把钥匙贴门上）；`--setup` 后也不往屏幕上回显。
  用 `--init` 就完全绕开 token。
- `tunnel list --output json` 已内置：换台电脑配时，能认出同账号里已有的隧道直接复用。
- 换域名/换隧道：`--setup` 重跑一次即可，token 会就地更新。
- 还没域名？先买一个（最便宜的 .com/.xyz 一年几十块），或者继续用 quick tunnel。
- 固定域名这条路**必须有自己托管在 Cloudflare 的域名**；没有就只能回到 quick tunnel
  （地址会变）。`--init` 里登录失败最常见的就是这个原因。

- 直接 `python app.py` 默认只监听 `127.0.0.1`，**别的设备根本连不上**。
- 网关口/HSTS：`SESSION_COOKIE_SECURE` 默认跟着 `BEHIND_TLS` 走，且按真实 scheme
  微调。所以用 http 局域网访问时把 `BEHIND_TLS=0`，否则浏览器不会存 cookie，
  表现成"密码对了还是进不去"。
- 多台设备共用一个出口 IP 时，登录限流按 **IP + 浏览器指纹** 分桶，
  一台输错几次不会把其余设备一起挡成 429。
- 长跑的正式环境（VPS / Docker）用 Caddy 自动证书：`Caddyfile` 直接
  `reverse_proxy app:8000` 即可，别把 5000 端口裸奔出去。

## 删除功能为什么会"点不动" / 报错

删除走的 `/api/delete`（CSRF 校验 + 删库 + 删磁盘 + 删缩略图）。排查顺序：

1. **提示"删除失败：403"** —— 页面比会话老。最常见：另一个标签页重新登录过，
   或者页面被浏览器缓存住了。按 `Ctrl+F5` 刷新即可。现在所有 HTML 都带
   `Cache-Control: no-store`，从源头避免这种情况。
2. **提示"操作被拒绝（HTTP 403）…正在回到密码页"** —— 登录态已失效，重输一次密码。
3. **提示"删除失败：CSRF 校验失败"** —— 服务端明确返回的原因，前端会原样显示，
   不再只弹一个状态码。
4. **提示"已删除 N 个，M 个已不存在"** —— 秒传造成的重复内容在库里只有一条记录，
   删别名时如实归类到 `missing`，不会假装删成功。
5. 删完刷新列表：库里、磁盘、缩略图三处一起清掉，不存在只删了数据库行的情况。

## 本地跑起来（不用 Docker）

```bash
python scripts/seed_demo.py 12         # 可选：生成 12 张演示图看效果
python app.py                          # → http://127.0.0.1:5000
```

`SECRET_KEY` 生成：`python -c "import secrets;print(secrets.token_hex(32))"`

生产环境一律走 gunicorn（见 Dockerfile），不要 `debug=True`。

---

Deployment: Docker + Nginx (TLS termination)

What I added
- Dockerfile: runs the Flask app with Gunicorn on port 8000.
- docker-compose.yml: defines two services: app and nginx. Nginx listens on 80/443 and proxies to the app service.
- nginx/ conf: example nginx config that redirects HTTP->HTTPS and proxies / to the Gunicorn app. TLS cert paths point to /etc/letsencrypt/live/<domain>/fullchain.pem.
- scripts/generate-self-signed-cert.sh: create a self-signed cert for local testing (drops into ./certs/live/<domain>/).
- .env.example: example environment file for docker-compose variables.

Quick start (testing with self-signed cert)
1. Copy the example env: cp .env.example .env
   - SECRET_KEY: openssl rand -hex 32
   - PASSWORD_HASH: python scripts/init_password.py （不要再写明文口令）
2. Generate a local self-signed cert (for testing):
   chmod +x scripts/generate-self-signed-cert.sh
   ./scripts/generate-self-signed-cert.sh example.com
   This writes cert files to ./certs/live/example.com/fullchain.pem and privkey.pem.
3. Update nginx/conf.d/site.conf: replace server_name example.com with the domain you used.
4. Start services:
   docker compose up -d --build
5. Visit:
   https://localhost (you may need to accept the self-signed cert in your browser) or https://example.com if DNS points to the host.

Production notes (Let's Encrypt)
- Obtain real certs for your domain and place them (or use a cert manager) under ./certs/live/<your-domain>/fullchain.pem and privkey.pem. Then docker-compose will make them available to nginx.
- Better: use an automated reverse-proxy + Let's Encrypt companion (nginx-proxy + docker-letsencrypt-nginx-proxy-companion) or use a host-managed certificate (cloud load balancer or Traefik/Caddy which integrate with ACME).
- Configure your DNS to point the domain to the host IP and run certbot to obtain certs, or use a managed solution.

Security reminders
- Do NOT use self-signed certs in production.
- Ensure SECRET_KEY and PASSWORD_HASH are strong and injected from a secure source (secrets manager / environment not committed to repo). Both are required: the app refuses to start if either is missing.
- Never run with `debug=True`. The Werkzeug debugger exposes an interactive console.
- Run tests before deploying: `python -m unittest discover tests`
- Use a hardened nginx config and enable HSTS only after confirming TLS works.
- Consider running the app under a non-root user inside the container and set proper file permissions for uploads.

If you want, I can:
- Add an automated nginx-proxy + Let's Encrypt companion docker-compose setup (zero-config for certs) OR
- Provide a Traefik-based docker-compose that automatically obtains/refreshes certificates from Let's Encrypt OR
- Create a cloud-specific deployment guide (e.g., Google Cloud Run, AWS ECS, DigitalOcean App Platform).

Tell me which option you prefer and I'll add it to the repo and push the changes.
