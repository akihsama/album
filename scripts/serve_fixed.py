#!/usr/bin/env python
"""用「自己的域名 + Cloudflare 命名隧道」把相册搬到公网，地址永远不变。

和 scripts/serve_public.py（免费 quick tunnel）的关系：

    serve_public.py  →  https://random-words.trycloudflare.com  重启就换地址
    serve_fixed.py   →  https://album.example.com               重启还是它

quick tunnel 的好处是零配置，代价是地址每次重启都变；固定域名正好反过来 ——
一次配好，之后永远同一个地址，手机/平板/家人都能存成书签。

它做了什么：
  1. 读 .env 里的 TUNNEL_TOKEN（Cloudflare 后台给的，不用我们自己去登录授权）；
     也可以用 TUNNEL_CRED_FILE + TUNNEL_ID（旧式 credentials-file 方式）;
  2. 生成一份 cloudflared 配置，把 PUBLIC_HOST 这个域名指到本机 127.0.0.1;
  3. 拉起相册（只绑回环，BEHIND_TLS=1），再拉起隧道；
  4. 顺手查一下这个域名在 DNS 里有没有指到 cfargotunnel.com —— 没指过去
     就明说「后台还没配好」，而不是让你对着一个打不开的地址猜半天。

关键安全点：
  · **token 只走环境变量，绝不出现在命令行里**。命令行会被任务管理器、
    ps aux、shell history 看见，等于把钥匙贴门上；
  · 这条路径依旧只把相册绑 127.0.0.1，对外入口只有隧道那一个。

用法：
    python scripts/serve_fixed.py --setup             # 第一次：填 token、生成配置、查 DNS
    python scripts/serve_fixed.py                     # 之后每次：直接开跑
    python scripts/serve_fixed.py --host album.example.com --port 5001
    python scripts/serve_fixed.py --check              # 只做体检，不起服务
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import threading
import queue
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve_common import (  # noqa: E402
    ENV_PATH, ROOT, ensure_env, find_cloudflared, pick_port, read_env,
    spawn, spawn_app, stop, wait_health, write_env,
)

WINDOWS = sys.platform.startswith("win")
DEFAULT_PORT = 5001          # 和 quick tunnel 那条一样，两条路互不干涉
DEFAULT_NAME = "album"
TOOLS_DIR = ROOT / "tools"
CLOUDFLARE_TARGET = "cfargotunnel.com"

# 隧道跑起来后 cloudflared 会打这几类行，挑重点转发给用户
LOG_MARKERS = ("ERR", "Error", "error", "INF", "cannot", "failed", "refused")


# --------------------------------------------------------------------- 配置生成

def render_config(port: int, host: str | None, tunnel_id: str | None = None,
                  cred_file: str | None = None) -> str:
    """手写一份最小 cloudflared 配置（不引 PyYAML，少一个依赖）。

    两种形态：
      · 只有 token：cloudflared 自己去环境变量 TUNNEL_TOKEN 取凭据，
        config 里只放「域名 → 本机端口」的路由；
      · 有 credentials-file：config 里写死 tunnel id 和凭据路径，
        连 token 都不用。

    末尾那个 http_status:404 不是装饰 —— 没匹配上 hostname 的请求必须有兜底，
    否则 cloudflared 会拒绝启动（它要求 ingress 最后一条是兜底规则）。
    """
    lines: list[str] = [
        "# 由 scripts/serve_fixed.py 生成，别手改（改了下次跑会被覆盖）",
        f"# 生成时间：{_now()}",
        "",
    ]
    if tunnel_id and cred_file:
        cred = str(cred_file).replace("\\", "/")
        lines += [f"tunnel: {tunnel_id}", f'credentials-file: "{cred}"', ""]
    lines.append(f"url: http://127.0.0.1:{port}")
    lines.append("ingress:")
    # 注意：Windows 路径里的反斜杠在 YAML 里是转义符，写成 C:\x\y.json 会被读坏。
    # 统一转成正斜杠 —— Windows API 从来都认 'C:/x/y.json'，YAML 里还不用转义。
    if host:
        lines.append(f"  - hostname: {host}")
        lines.append(f"    service: http://127.0.0.1:{port}")
    lines.append("  - service: http_status:404")
    return "\n".join(lines) + "\n"


def _now() -> str:
    import datetime

    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def config_path(name: str = DEFAULT_NAME) -> Path:
    return TOOLS_DIR / f"tunnel-{name}.yml"


def write_config(text: str, name: str = DEFAULT_NAME) -> Path:
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    p = config_path(name)
    # 先写临时文件再替换：配置写一半被中断会留下半份 yaml，
    # cloudflared 读到一个残缺文件只会干瞪眼
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)
    if not WINDOWS:
        p.chmod(0o600)      # 配置里可能带凭据路径，不给旁人读
    return p


# --------------------------------------------------------------------- DNS 体检

def resolve_cname(hostname: str, timeout: int = 15) -> str | None:
    """查 hostname 的 CNAME 链，返回它最终指向的名字（查不到返回 None）。

    优先用系统自带工具：Windows 有 nslookup，macOS/Linux 常见有 dig。
    都不想引第三方依赖（dnspython 也要装），所以宁可 subprocess 一下。
    """
    probes = []
    if shutil.which("nslookup"):
        probes.append(["nslookup", "-type=CNAME"])
    if shutil.which("dig"):
        probes.append(["dig", "+short", "CNAME"])
    for head in probes:
        try:
            out = subprocess.run(head + [hostname], capture_output=True,
                                 timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        # 中文 Windows 的 nslookup 吐的是 GBK，直接当 utf-8 读会在 reader 线程里炸，
        # 炸完 stdout 只剩半截 —— 表现为"明明配了却查不到 CNAME"。所以手动解码。
        raw = (out.stdout or b"") + (out.stderr or b"")
        text = raw.decode("gbk" if WINDOWS else "utf-8", errors="replace")
        hit = None
        for line in text.splitlines():
            low = line.lower()
            # nslookup: "www.github.com\tcanonical name = abc.cfargotunnel.com"
            if "canonical name" in low and "=" in line:
                hit = line.split("=", 1)[-1].strip()
                break
            # dig +short: "abc.cfargotunnel.com"  —— 首行就是答案，且以点结尾
            if hit is None and low.endswith(".") and " " not in line.strip().rstrip("."):
                cand = line.strip().rstrip(".")
                if cand and cand != hostname and cand != "127.0.0.1":
                    hit = cand
        if hit:
            # 域名末尾那个点（FQDN）是 DNS 的写法，比对时要去掉
            return hit.rstrip(".")
    return None


def check_host(host: str | None) -> int:
    """DNS 体检。返回 0=没问题 1=没配好。"""
    if not host:
        print("  · 没设 PUBLIC_HOST，跳过 DNS 体检（纯 quick 式用法）")
        return 0
    print(f"\n  · 查 DNS：{host} 的 CNAME …")
    try:
        target = resolve_cname(host)
    except Exception as e:                      # 没网/没工具也不能崩
        print(f"    （查不了：{e}，DNS 体检跳过）")
        return 0
    if target is None:
        print(f"    ✗ 查不到 CNAME —— 多半是域名还没在 DNS 里指过来。")
        print("      去 Cloudflare 后台：Zero Trust → Networks → Tunnels → 你的隧道")
        print("      → Public hostname → Add a public hostname，填这个域名；")
        print("      它会让你加一条 CNAME 指向 <隧道ID>.cfargotunnel.com，照做就好。")
        return 1
    if target.lower().endswith(CLOUDFLARE_TARGET):
        print(f"    ✓ 已指向 {target}")
        return 0
    print(f"    ✗ 指向了 {target}，不是 {CLOUDFLARE_TARGET} —— 隧道流量到不了。")
    print("      把 CNAME 改到 <你的隧道ID>.cfargotunnel.com（在隧道详情里能看到 ID）。")
    return 1


# --------------------------------------------------------------------- 启动

def pump_logs(proc: subprocess.Popen, q: queue.Queue) -> None:
    """把 cloudflared 的输出往队列里泵（和 serve_public 同样的坑：不能 readline 死等）。"""
    for s in (proc.stderr, proc.stdout):
        if s is None:
            continue
        try:
            for line in iter(s.readline, ""):
                q.put(line)
        except (ValueError, OSError):
            pass


def run_tunnel(cf: Path, cfg: Path, env: dict[str, str] | None = None,
               timeout: int = 45) -> tuple[str, subprocess.Popen | None]:
    """拉起隧道，等它连上。

    返回 (状态行, 进程句柄) —— 进程句柄要给出去，Ctrl+C 时才收得掉自己拉起的东西。
    """
    cf_proc = spawn([str(cf), "tunnel", "--config", str(cfg), "run"], env_extra=env, pipe=True)
    q: queue.Queue = queue.Queue()
    threading.Thread(target=pump_logs, args=(cf_proc, q), daemon=True).start()

    deadline = time.time() + timeout
    found = ""
    while time.time() < deadline and cf_proc.poll() is None:
        try:
            line = q.get(timeout=0.3)
        except queue.Empty:
            continue
        if not line:
            continue
        if any(k in line for k in LOG_MARKERS) or "INF" in line:
            print("    " + line.rstrip()[:170], flush=True)
        low = line.lower()
        if not found and ("connected" in low or "registered in dns" in low
                          or "your tunnel" in low):
            found = line.strip()
    return found, cf_proc


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="用固定域名 + 命名隧道跑相册")
    ap.add_argument("--setup", action="store_true", help="第一次配置：填 token、生成配置、查 DNS")
    ap.add_argument("--check", action="store_true", help="只体检（token/配置/DNS），不起服务")
    ap.add_argument("--host", help="你的固定域名，如 album.example.com")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    ap.add_argument("--password", help="顺手把口令设成这个值")
    ap.add_argument("--token", help="Cloudflare 给的隧道 token（不给则 --setup 时手输）")
    ap.add_argument("--name", default=DEFAULT_NAME, help=f"隧道名，默认 {DEFAULT_NAME}")
    args = ap.parse_args()

    print("=" * 66)
    print("  相册 · 固定地址（Cloudflare 命名隧道 + 你自己的域名）")
    print("=" * 66)

    saved = read_env()

    # 1) 口令和密钥先备齐，免得相册起来才炸
    ensure_env(need_password=not args.password, password=args.password)

    host = args.host or saved.get("PUBLIC_HOST") or None
    name = args.name or saved.get("TUNNEL_NAME") or DEFAULT_NAME
    token = args.token or saved.get("TUNNEL_TOKEN") or None
    cred_file = saved.get("TUNNEL_CRED_FILE") or None
    tunnel_id = saved.get("TUNNEL_ID") or None

    if args.check and token and not args.setup:
        # --check 只体检：连 .env 里的配置一并确认一遍，不碰服务
        bad = check_host(host)
        print("\n  · token 已就位（长度 %d，来源：%s）"
              % (len(token), "命令行 --token" if args.token else ".env"))
        return 1 if bad else 0

    # 2) --setup：把 token 存进 .env，生成配置，做 DNS 体检
    if args.setup:
        if not token:
            print("\n  在 Cloudflare 后台拿 token：")
            print("    Zero Trust → Networks → Tunnels → Create tunnel → 名字填 album")
            print("    → 选 Cloudflare 里的『Cloudflare 无客户端连接』→ 下一步")
            print("    页面会给你一长串 token（copy 按钮复制），粘到下面：")
            token = getpass.getpass("  token（输入不回显，直接 Ctrl+V 后回车）：").strip()
        if not token:
            print("\n✗ 没拿到 token，先去 Cloudflare 建一个隧道再回来。")
            return 2
        if not host:
            host = input("  固定域名（如 album.example.com，必须有自己的域名）：").strip()
        if not host:
            print("\n✗ 固定域名不能为空 —— 没有域名就没法固定地址。")
            return 2

        write_env({"TUNNEL_TOKEN": token, "PUBLIC_HOST": host, "TUNNEL_NAME": name})
        # token 只留在 .env，压根不回显、不写进任何命令行
        print(f"\n  · token 已存进 .env（只写不打印）；域名 {host}")
        cfg = write_config(render_config(args.port, host))
        print(f"  · 配置已生成：{cfg}")
        cred_file = None
        tunnel_id = None

    if token and cred_file:
        print("\n✗ TUNNEL_TOKEN 和 TUNNEL_CRED_FILE 只能给一种，两个都配了让 cloudflared 怎么选？")
        return 2

    if not token and not (tunnel_id and cred_file):
        print("\n✗ 还没配好：.env 里需要下面任意一个")
        print("    TUNNEL_TOKEN=<cloudflared 给的 token>          ← 推荐，最省事")
        print("    TUNNEL_ID=<隧道UUID> + TUNNEL_CRED_FILE=<路径>  ← 旧式凭据文件")
        print("  跑：python scripts/serve_fixed.py --setup")
        return 2

    cf = find_cloudflared()
    if cf is None:
        print("\n✗ 没找到 cloudflared。先跑一次：python scripts/serve_public.py")
        print("  它会下载一个到 tools/，之后两条路共用。")
        return 2

    print(f"  · cloudflared: {cf}")

    if not cred_file:
        cfg = write_config(render_config(args.port, host))
    else:
        cfg = write_config(render_config(args.port, host, tunnel_id, cred_file))

    port = pick_port(DEFAULT_PORT, args.port)
    if port != args.port:
        print(f"  · 端口 {args.port} 被占，改用 {port}")

    bad = check_host(host)
    if bad:
        print("  （DNS 还没通，但先起服务看看 —— 隧道自己会报错，比我们拦着更有信息量）")

    app_proc = spawn_app(port, "127.0.0.1", {"BEHIND_TLS": "1"})
    cf_proc = None
    try:
        if not wait_health(port):
            print(f"\n✗ 本地相册没起来（{port} 端口 30 秒无响应），上面的报错才是重点。")
            return 1

        print("  · 正在连接隧道 …")
        # token 走环境变量，命令行里一行都看不到
        env = {"TUNNEL_TOKEN": token} if token else None
        note, cf_proc = run_tunnel(cf, cfg, env)

        print("\n" + "=" * 66, flush=True)
        if host:
            print("  固定地址（以后重启还是它）：", flush=True)
            print(f"  https://{host}", flush=True)
        else:
            print("  隧道已连上（这次没设域名，用后台配的公共主机名）：", flush=True)
        print("=" * 66, flush=True)
        if note:
            print(f"  · 隧道状态：{note[-90:]}", flush=True)
        print("\n  · BEHIND_TLS=1：会话 cookie 带 Secure，https 下登录态才留得住。", flush=True)
        print("  · Ctrl+C 结束，只收自己拉起的这两个进程。\n", flush=True)

        while cf_proc is None or cf_proc.poll() is None:
            time.sleep(1)
        return 0
    except KeyboardInterrupt:
        print("\n正在关闭 …")
        return 0
    finally:
        stop(cf_proc)
        stop(app_proc)


if __name__ == "__main__":
    raise SystemExit(main())
