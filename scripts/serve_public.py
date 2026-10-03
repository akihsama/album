#!/usr/bin/env python
"""把相册安全地搬到公网：一条命令拿到 https 地址，别人只输一次密码就能用。

为什么是 Cloudflare Tunnel：
  · 免费、不用注册、不用买域名；
  · 不用在你家/机房路由器上做端口映射，也就不暴露真实 IP；
  · 自动签 HTTPS 证书 —— 这是硬要求：BEHIND_TLS=1 时会话 cookie 带 Secure，
    只有 https 才发得出去。裸 http 上公网等于把密码明文甩在带宽里。

它做了什么：
  1. 检查 .env（PASSWORD_HASH / SECRET_KEY），缺 SECRET_KEY 自动补，缺口令就拒绝启动；
  2. 在本机拉起 app.py（**只绑 127.0.0.1**，不对外裸奔）；
  3. 拉起 cloudflared tunnel 指向它，从输出里抓出 https 地址；
  4. Ctrl+C 时只收自己拉起的那两个进程。

和局域网那条路（scripts/serve_lan.py）互不干涉：默认端口 5001 对 5000，
两边甚至可以同时开着跑，各自连同一份相册数据，谁也不抢谁的端口、谁的进程。

用法：
    python scripts/serve_public.py                        # 一键公网（临时 https 地址）
    python scripts/serve_public.py --password 我的口令     # 顺手把口令设成这个
    python scripts/serve_public.py --port 5001

想固定域名（不每次重启都变）见 README「固定公网地址」一节。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve_common import (  # noqa: E402
    ROOT, ensure_env, pick_port, port_busy, spawn, spawn_app, stop, wait_health,
)

WINDOWS = sys.platform.startswith("win")
CF_URL_RE = re.compile(r"https://[A-Za-z0-9\-]+\.trycloudflare\.com")
UPLOADS = ROOT / "uploads"
DEFAULT_PORT = 5001          # 局域网那条用的是 5000，别撞车
# 公网那侧的 TLS 语义：隧道终结 TLS，必须让 app 知道，
# 否则它会按 http 下发不带 Secure 的会话 cookie（有测试盯着这条）
PUBLIC_TLS_ENV = {"BEHIND_TLS": "1"}


# --------------------------------------------------------------------------- cloudflared

def _uname_arch() -> str:
    try:
        out = subprocess.run(["uname", "-m"], capture_output=True, text=True, timeout=5)
        m = out.stdout.strip()
    except Exception:
        return "amd64"
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(m, "amd64")


def _release_asset_url() -> str:
    """最新版下载地址。

    用 releases/latest/download/<asset> 而不是写死版本号 —— GitHub 会 302 到最新，
    写死的版本号一旦被删就成了 404（踩过）。
    """
    system = "windows" if WINDOWS else sys.platform
    if system == "darwin":
        arch = _uname_arch()
    elif WINDOWS:
        arch = "amd64"
    else:
        arch = _uname_arch()
    return (f"https://github.com/cloudflare/cloudflared/releases/latest/download/"
            f"cloudflared-{system}-{arch}{'.exe' if WINDOWS else ''}")


def download_cloudflared(dest: Path) -> Path:
    url = _release_asset_url()
    print(f"  · 下载 cloudflared → {dest.name}")
    tmp = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r      {done/1048576:.1f}/{total/1048576:.1f} MB  {done*100//total}%", end="", flush=True)
            print()
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"✗ 下载失败：{e}\n  手动装也行：https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/install-and-setup/installation/")
    tmp.replace(dest)
    if not WINDOWS:
        dest.chmod(0o755)
    return dest


def find_cloudflared() -> Path | None:
    """按顺序找：环境变量 → PATH → 常用目录 → tools/。"""
    if os.environ.get("CLOUDFLARED"):
        p = Path(os.environ["CLOUDFLARED"])
        if p.exists():
            return p
    hit = shutil.which("cloudflared")
    if hit:
        return Path(hit)
    exe = "cloudflared.exe" if WINDOWS else "cloudflared"
    for c in (ROOT / "tools" / exe,
              Path.home() / ".cloudflared" / exe,
              Path.home() / "AppData" / "Local" / "Programs" / "Cloudflare Tunnel" / exe,
              ROOT / exe):
        if c.exists():
            return c
    return None


def grab_tunnel_url(proc: subprocess.Popen, timeout: int = 60, verbose: bool = False) -> str:
    """从 cloudflared 输出里抓 https 地址（它把日志打到 stderr）。

    两个坑都绕开了：
      1. 不能用 readline() 死等 —— 网络受限时 cloudflared 会一直不吐数据，
         主线程会永久阻塞在那一行的 read 上，连超时都触发不了；
      2. 管道输出是块缓冲的，得 flush 才能让用户实时看到地址。
    所以开一个守护线程专门泵输出，主线程按超时从队列里取。
    """
    q = queue.Queue()

    def pump() -> None:
        for s in (proc.stderr, proc.stdout):
            if s is None:
                continue
            try:
                for line in iter(s.readline, ""):
                    q.put(line)
            except (ValueError, OSError):
                pass

    threading.Thread(target=pump, daemon=True).start()

    deadline = time.time() + timeout
    while time.time() < deadline and proc.poll() is None:
        try:
            line = q.get(timeout=0.3)
        except queue.Empty:
            continue
        if not line:
            continue
        m = CF_URL_RE.search(line)
        if m:
            return m.group(0)
        if verbose or any(k in line for k in ("INF", "WARN", "ERR", "Error", "error")):
            print("    " + line.rstrip()[:160], flush=True)
    return ""


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="用 Cloudflare Tunnel 把相册安全暴露到公网")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)),
                    help=f"本机侧端口，默认 {DEFAULT_PORT}（局域网那条用 5000）")
    ap.add_argument("--password", help="顺手把口令设成这个值（写进 .env 的 PASSWORD_HASH）")
    ap.add_argument("--skip-download", action="store_true", help="cloudflared 不存在时直接报错退出")
    args = ap.parse_args()

    print("=" * 66)
    print("  相册 · 公网访问（Cloudflare Tunnel，自动 HTTPS）")
    print("=" * 66)

    # 1) 先把口令和密钥备好，免得 app 起来了才发现启动即退出
    ensure_env(need_password=not args.password, password=args.password)

    # 2) 端口：被占就顺延，别跟局域网那条撞
    port = pick_port(DEFAULT_PORT, args.port)
    if port != args.port:
        print(f"  · 端口 {args.port} 被占，改用 {port}")

    # 3) cloudflared
    cf = find_cloudflared()
    if cf is None:
        if args.skip_download:
            print("\n✗ 没找到 cloudflared。装上：https://cloudflare.com/cloudflared")
            return 2
        cf_dir = ROOT / "tools"
        cf_dir.mkdir(exist_ok=True)
        cf = download_cloudflared(cf_dir / ("cloudflared.exe" if WINDOWS else "cloudflared"))
    print(f"  · cloudflared: {cf}")

    # 4) 本地起 app（只绑回环，公网入口由 tunnel 提供）
    UPLOADS.mkdir(exist_ok=True)
    # 公网走 https（隧道终结 TLS），必须让 app 知道，否则会下发不带 Secure 的 cookie
    app_proc = spawn_app(port, "127.0.0.1", PUBLIC_TLS_ENV)
    cf_proc = None
    try:
        if not wait_health(port):
            print(f"\n✗ 本地相册没起来（{port} 端口 30 秒内没响应 /healthz），看上面的报错。")
            return 1

        print("  · 正在建立公网隧道（首次几秒）…")
        cf_proc = spawn([str(cf), "tunnel", "--url", f"http://127.0.0.1:{port}"], pipe=True)
        url = grab_tunnel_url(cf_proc)

        if not url:
            print("\n✗ 没抓到隧道地址，多半是这台机器访问 Cloudflare 被挡了（公司网络常见）。")
            print("  手动排一次：")
            print(f"    {cf} tunnel --url http://127.0.0.1:{port}")
            return 1

        # 全部 flush：地址要是留在缓冲区里，进程一被中断（Ctrl+C / 被回收）就白打印了
        print("\n" + "=" * 66, flush=True)
        print("  公网地址（浏览器打开，输一次密码就进）：", flush=True)
        print(f"  {url}", flush=True)
        print("=" * 66, flush=True)
        print("\n  · 这是 https：密码和照片都加密传输，会话 cookie 带 Secure 标记。", flush=True)
        print("  · 免费版每次重启地址会变；要固定地址看 README「固定公网地址」一节。", flush=True)
        print("  · Ctrl+C 结束，两个进程都会收干净。\n", flush=True)

        while cf_proc.poll() is None:
            time.sleep(1)
        return cf_proc.returncode or 0
    except KeyboardInterrupt:
        print("\n正在关闭 …")
        return 0
    finally:
        # 只收自己拉起的进程，绝不碰局域网那条路的服务
        stop(cf_proc)
        stop(app_proc)


if __name__ == "__main__":
    raise SystemExit(main())
