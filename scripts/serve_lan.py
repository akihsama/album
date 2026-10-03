#!/usr/bin/env python
"""在「同一个 WiFi 下的别的终端 / 手机」上打开相册：只监听一次密码。

默认 python app.py 只绑 127.0.0.1，同一台电脑以外的设备根本连不上。
这个脚本负责：

  1. 找出局域网里的本机地址，拼出可以直接点开的地址（终端里按住 Ctrl 就能点）；
  2. 以 HOST=0.0.0.0 拉起 app.py，关掉时一起收干净；
  3. 把安全边界讲清楚：局域网内谁都能连，但 / 只有密码门。

用法（三条路互不干涉，各自都能单独跑）：
    python scripts/serve_lan.py                # 局域网（手机/平板/另一台电脑），默认端口 5000
    python scripts/serve_lan.py --port 5000    # 换端口（公网那条用的是 5001，不打架）
    python scripts/serve_lan.py --loopback     # 只在本机开，用来验证配置

注意：局域网暴露只对可信 WiFi 成立。别在同一个 WiFi 下也用公网那条 —— 那是给
「不在家里网络」用的。跨互联网访问走 scripts/serve_public.py（Cloudflare Tunnel）。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve_common import (  # noqa: E402
    ROOT, ensure_env, port_busy, spawn_app, stop, wait_health,
)


def lan_ips() -> list[str]:
    """不依赖 psutil：开一个 UDP 连接问内核「出网会用哪个地址」。"""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        local = s.getsockname()[0]
        s.close()
        if local.startswith("127.") or local.startswith("169.254."):
            local = ""
        if local:
            ips.append(local)
    except OSError:
        pass

    # 再兜一遍所有已配网卡，避免上一步因为没外网而拿不到地址
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith(("127.", "0.", "169.254.", "::")):
                continue
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def main() -> int:
    ap = argparse.ArgumentParser(description="在多终端上启动相册（只输密码即可进入）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")),
                    help="默认 5000（公网那条路用 5001，两边不会抢）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--loopback", action="store_true", help="只监听本机，用来验证配置")
    args = ap.parse_args()

    host = "127.0.0.1" if args.loopback else args.host
    port = args.port

    print("=" * 62)
    print("  相册 · 多终端启动（同一 WiFi）")
    print("=" * 62)
    ensure_env(need_password=True)

    # 端口被占就别硬顶，说清楚是谁占的、怎么办
    if port_busy(port):
        print(f"\n✗ 端口 {port} 已经被占用了（多半是另一条路还在跑，或上次没关干净）。")
        print("  换一个：python scripts/serve_lan.py --port 5100")
        print("  看是谁占的：netstat -ano | grep :%d" % port)
        return 2

    ips = [] if args.loopback else lan_ips()
    urls = [f"http://127.0.0.1:{port}"] + [f"http://{ip}:{port}" for ip in ips]

    print("\n在这些地址打开，输入一次密码即可进入（Ctrl+点击就能开）：")
    for u in urls:
        mark = "  ← 手机/其他电脑用这个" if (ips and u.endswith(f"{ips[0]}:{port}")) else ""
        print(f"  {u}{mark}")

    if not args.loopback:
        print("\n安全提示：")
        print("  · 这个端口对同一局域网内所有设备可见，请只在可信 WiFi 下用")
        print("  · 根地址只有密码门，没有密码看不到任何内容")
        print("  · 要跨互联网访问（不在同一个 WiFi），用 scripts/serve_public.py "
              "（Cloudflare Tunnel 自动 HTTPS，别裸奔端口）")
        if sys.platform.startswith("win"):
            print("  · Windows 首次会弹防火墙提示：允许专用网络（不要勾公用网络）")

    print(f"\n正在启动（监听 {host}:{port}）… Ctrl+C 结束\n")

    proc = spawn_app(port, host)
    try:
        if not wait_health(port):
            print(f"\n✗ 服务没起来（{port} 端口 30 秒内没响应 /healthz），看上面的报错。")
            stop(proc)
            return 1
        return proc.wait()
    except KeyboardInterrupt:
        print("\n正在关闭 …")
        return 0
    finally:
        stop(proc)


if __name__ == "__main__":
    raise SystemExit(main())
