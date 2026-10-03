"""serve_lan.py（局域网）与 serve_public.py（公网）共用的小工具。

两个脚本刻意做成**互不干涉、各自都能独立跑通**：

  · 都只用这里的同一套检查（.env 是否就绪、端口是否被占、进程怎么收），
    但谁都不去碰对方的状态 —— 不写对方要的端口、不杀对方拉起的进程；
  · 默认端口不同（局域网 5000 / 公网本机侧 5001），同时开两个也不冲突；
  · 都只认自己 subprocess 返回的 Popen 句柄做清理，退出时互不残留孤儿进程。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
WINDOWS = sys.platform.startswith("win")


# --------------------------------------------------------------------- .env 读写

def read_env() -> dict[str, str]:
    """极简 .env 解析（跟 app.py 自己读法一致，两边别各读各的）。"""
    data: dict[str, str] = {}
    if not ENV_PATH.exists():
        return data
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def write_env(patch: dict[str, str]) -> None:
    """保留原有注释和顺序，只改/补指定键（不会把 .env 重写成一块死板）。"""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in patch:
                out.append(f"{k}={patch[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in patch.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def ensure_env(need_password: bool = False, password: str | None = None) -> None:
    """启动前把「app.py 一跑就 SystemExit」的坑全挡在门外。"""
    if password:
        from argon2 import PasswordHasher

        write_env({"PASSWORD_HASH": PasswordHasher().hash(password)})
        print("  · 已把新口令写进 .env 的 PASSWORD_HASH（明文口令依旧不入库）")
        return

    env = read_env()
    missing = [k for k, need in (("PASSWORD_HASH", need_password), ("SECRET_KEY", True)) if need and not env.get(k)]
    if need_password and not env.get("PASSWORD_HASH"):
        print("\n✗ .env 里没有 PASSWORD_HASH，app.py 会直接 refuse 启动（安全设计：绝不吃明文口令）。")
        print("  二选一：")
        print("    python scripts/init_password.py                       # 自己敲一遍")
        print("    python scripts/serve_public.py --password 你的口令     # 一条命令设好")
        raise SystemExit(2)
    if not env.get("SECRET_KEY"):
        import secrets

        write_env({"SECRET_KEY": secrets.token_urlsafe(32)})
        print("  · .env 缺 SECRET_KEY，已补一个随机值（会话签名靠它，别再删）")


# --------------------------------------------------------------------- cloudflared

def find_cloudflared() -> Path | None:
    """按顺序找 cloudflared：环境变量 → PATH → 常用目录 → tools/。

    两条路（quick tunnel / 固定域名）都用同一个查找结果，
    免得一份逻辑复制两遍、改了一处另一处还按旧路径找。
    """
    if os.environ.get("CLOUDFLARED"):
        p = Path(os.environ["CLOUDFLARED"])
        if p.exists():
            return p
    hit = shutil.which("cloudflared")
    if hit:
        return Path(hit)
    exe = "cloudflared.exe" if WINDOWS else "cloudflared"
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        home = None          # 容器/挂了 HOME 的环境下不强求
    for c in (ROOT / "tools" / exe,
              home / ".cloudflared" / exe if home else None,
              home / "AppData" / "Local" / "Programs" / "Cloudflare Tunnel" / exe if home else None,
              ROOT / exe):
        if c is not None and c.exists():
            return c
    return None


# --------------------------------------------------------------------- 端口

def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否已被占用（含 0.0.0.0 绑的那个 —— 连 127.0.0.1 会通）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def pick_port(start: int, prefer: int) -> int:
    """prefer 被占就顺延找空位；两个脚本并跑也不会互相抢端口。"""
    p = prefer
    while port_busy(p) and p < start + 100:
        p += 1
    return p


# --------------------------------------------------------------------- 进程

def spawn(cmd: list[str], env_extra: dict[str, str] | None = None, cwd: Path | None = None,
          pipe: bool = False) -> subprocess.Popen:
    """pipe=True 时才能 readline() 读它的输出（cloudflared 就得这样抓地址）。"""
    kw: dict = {"cwd": cwd or ROOT, "text": True}
    if env_extra:
        kw["env"] = dict(os.environ, **env_extra)
    if pipe:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if WINDOWS:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


def spawn_app(port: int, host: str = "127.0.0.1", env_extra: dict[str, str] | None = None) -> subprocess.Popen:
    """拉起相册本体。两条路都只绑回环，公网入口由 tunnel 提供。

    behind_tls 必须由调用方显式给出：局域网是 http、公网是 https，
    撒错了结果就是公网下发不带 Secure 的 cookie（密码在明文通道里跑）。
    """
    env = {"HOST": host, "PORT": str(port),
           "BEHIND_TLS": "0", "PYTHONUNBUFFERED": "1"}
    env.update(env_extra or {})
    return spawn([sys.executable, "app.py"], env)


def wait_health(port: int, timeout: int = 30) -> bool:
    """等 /healthz（免鉴权），比干等几秒可靠。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def stop(proc: subprocess.Popen | None) -> None:
    """只收自己拉起的进程，绝不去动别人的。"""
    if proc is None or proc.poll() is not None:
        return
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"], capture_output=True)
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
