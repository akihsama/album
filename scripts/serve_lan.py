#!/usr/bin/env python
"""让相册可以在「别的终端 / 手机」上直接打开：只监听一次密码。

默认 python app.py 只绑 127.0.0.1，同一台电脑以外的设备根本连不上。
这个脚本负责：

  1. 找出局域网里的本机地址，拼出可以直接点开的地址；
  2. 生成二维码，手机扫一下（同一 WiFi 下）就能开；
  3. 以 HOST=0.0.0.0 拉起 app.py，关掉时一起收干净；
  4. 把安全边界讲清楚：局域网内谁都能连，但 / 只有密码门。

用法：
    python scripts/serve_lan.py              # 局域网（手机/平板/另一台电脑）
    python scripts/serve_lan.py --qr         # 额外打印二维码（自动装 qrcode，装不上就跳过）
    python scripts/serve_lan.py --port 8080
    python scripts/serve_lan.py --loopback   # 只在本机开，用来验证配置

注意：局域网暴露只对可信 WiFi 成立。要跨互联网访问就用 deploy_cloudflare.sh
或 Caddy 自动证书，别直接把 5000 端口裸奔出去。
"""

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def lan_ips() -> list[str]:
    """不依赖 psutil：开一个 UDP 连接问内核「出网会用哪个地址」。"""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        local = s.getsockname()[0]
        s.close()
        if local.startswith("127."):
            local = ""
        if local:
            ips.append(local)
    except OSError:
        pass

    # 再兜一遍所有已配网卡，避免上一步因为没外网而拿不到地址
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("0.") or ip.startswith("169.254."):
                continue
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def print_qr(text: str) -> None:
    try:
        import qrcode  # type: ignore
    except ImportError:
        print("\n（装个二维码更方便手机扫：pip install qrcode）")
        return
    print("\n手机扫这个：")
    print(qrcode.make(text))
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="在多终端上启动相册（只输密码即可进入）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--qr", action="store_true", help="打印访问二维码")
    ap.add_argument("--loopback", action="store_true", help="只监听本机，用于调试")
    args = ap.parse_args()

    host = "127.0.0.1" if args.loopback else args.host
    port = args.port

    print("=" * 62)
    print("  相册 · 多终端启动")
    print("=" * 62)

    # 首选局域网地址（手机同一 WiFi 直接开）
    ips = [] if args.loopback else lan_ips()
    urls = [f"http://127.0.0.1:{port}"] + [f"http://{ip}:{port}" for ip in ips]

    print("\n在这些地址打开，输入一次密码即可进入：")
    for u in urls:
        mark = "  ← 手机/其他电脑用这个" if (ips and u.endswith(f"{ips[0]}:{port}")) else ""
        print(f"  {u}{mark}")

    if args.qr and urls:
        print_qr(urls[1] if len(urls) > 1 else urls[0])

    if not args.loopback:
        print("\n安全提示：")
        print("  · 这个端口对同一局域网内所有设备可见，请只在可信 WiFi 下用")
        print("  · 根地址只有密码门，没有密码看不到任何内容")
        print("  · 要跨互联网访问，走 deploy_cloudflare.sh 或 Caddy（自动 HTTPS）")
        if sys.platform.startswith("win"):
            print("  · Windows 首次会弹防火墙提示：允许专用网络（不要勾公用网络）")

    env = dict(os.environ, HOST=host, PORT=str(port), PYTHONUNBUFFERED="1")
    print(f"\n正在启动（监听 {host}:{port}）… Ctrl+C 结束\n")

    child = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env)
    try:
        rc = child.wait()
    except KeyboardInterrupt:
        print("\n正在关闭 …")
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
        rc = 0
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
