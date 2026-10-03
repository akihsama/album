"""固定域名（命名隧道）那条路的回归测试。

重点守四件事：
  1. 生成的 cloudflared 配置能直接被读（hostname 路由 + 404 兜底，缺兜底会启动失败）；
  2. Windows 路径里的反斜杠不会把 YAML 弄坏；
  3. **token 只进环境变量、绝不进命令行 / 打印** —— 命令行会被任务管理器和
     shell history 看见，漏出去等于把钥匙贴门上；
  4. DNS 体检认得出「已指向 cfargotunnel.com」和「压根没配」，且在中文 Windows 的
     GBK 输出下不能静默失效。
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import serve_common as SC  # noqa: E402
import serve_fixed as F  # noqa: E402

TOKEN = "AABBCC_token_不打印出来_9f3c9a7b1d2e"


def _fake_getpass(value: str):
    """main 里写的是 getpass.getpass(...)，所以假的也得是个「模块」。"""
    module = mock.MagicMock()
    module.getpass.return_value = value
    return module


def _dead_tunnel_proc():
    """跑完就该退出的假隧道进程：poll() 非 None，好让主循环的 while 转一圈就出来。"""
    proc = mock.MagicMock()
    proc.poll.return_value = 0
    return proc


class ConfigRenderTest(unittest.TestCase):
    def test_host_route_and_404_fallback(self):
        """cloudflared 要求 ingress 最后一条是兜底，少一条它直接拒绝启动。"""
        text = F.render_config(5001, "album.example.com")
        self.assertIn("hostname: album.example.com", text)
        self.assertIn("service: http://127.0.0.1:5001", text)
        self.assertIn("- service: http_status:404", text)

    def test_port_follows_argument(self):
        text = F.render_config(5070, "album.example.com")
        self.assertIn("127.0.0.1:5070", text)

    def test_port_not_hardcoded(self):
        """别把端口写死在生成物里 —— 端口被占时脚本会顺延。"""
        self.assertNotEqual(F.render_config(5071, "a.example.com"),
                            F.render_config(5001, "a.example.com"))

    def test_without_host_still_has_fallback(self):
        text = F.render_config(5001, None)
        self.assertNotIn("hostname:", text)
        self.assertIn("- service: http_status:404", text,
                      "没 hostname 也必须有兜底，否则 cloudflared 起不来")

    def test_credentials_path_is_yaml_safe(self):
        """Windows 的 C:\\x\\y 在 YAML 里反斜杠是转义符，必须转成 / 并加引号。"""
        text = F.render_config(5001, "album.example.com", "uuid-1111", r"C:\Users\me\.cloudflared\id.json")
        self.assertIn('credentials-file: "C:/Users/me/.cloudflared/id.json"', text)
        self.assertNotIn('C:\\Users', text, "反斜杠留在 yaml 里会被解析坏")

    def test_tunnel_id_present_in_cred_mode(self):
        text = F.render_config(5001, None, "uuid-2222", "/tmp/id.json")
        self.assertIn("tunnel: uuid-2222", text)

    def test_cred_mode_keeps_fallback(self):
        text = F.render_config(5001, None, "uuid-3333", "/tmp/id.json")
        self.assertIn("- service: http_status:404", text)


class ConfigWriteTest(unittest.TestCase):
    def test_writes_and_leaves_no_tmp(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(F, "TOOLS_DIR", tmp):
            p = F.write_config(F.render_config(5001, "album.example.com"))
        self.assertTrue(p.exists())
        self.assertEqual([q.name for q in tmp.iterdir()], ["tunnel-album.yml"],
                         "临时文件没清干净，下次启动会读到残缺配置")


class DnsCheckTest(unittest.TestCase):
    """DNS 体检：认得对、也认得出没配，且中文 GBK 输出下不能哑掉。"""

    NSLOOKUP_OK = ("服务器:  UnKnown\r\n"
                   "Address:  fe80::1\r\n"
                   "\r\n"
                   "album.example.com\tcanonical name = abc123.cfargotunnel.com\r\n")

    def _run_with(self, stdout: bytes):
        """把 nslookup 的输出钉死，看解析结果（一律用 bytes：真进程就是 bytes）。"""

        def fake_run(cmd, **kw):
            # 注意：类体里访问外层变量要靠显式赋值，写 "stdout = stdout" 会 NameError
            result = type("R", (), {"stdout": stdout, "stderr": b"", "returncode": 0})
            return result()

        with mock.patch.object(F.shutil, "which", return_value="/usr/bin/nslookup"), \
                mock.patch.object(F.subprocess, "run", side_effect=fake_run):
            return F.resolve_cname("album.example.com")

    def test_parses_gbk_canonical_name(self):
        """中文 Windows 的 nslookup 是 GBK；当 utf-8 读会静默解析失败。"""
        raw = self.NSLOOKUP_OK.encode("gbk")
        self.assertEqual(self._run_with(raw), "abc123.cfargotunnel.com")

    def test_returns_none_when_no_canonical(self):
        """域名不存在时只有 SOA，没有 canonical name —— 得如实返回 None 好让人去配。"""
        raw = b"example.com\r\n\tprimary name server = elliott.ns.cloudflare.com\r\n"
        self.assertIsNone(self._run_with(raw))

    def test_strips_trailing_dot(self):
        self.assertEqual(self._run_with(b"abc.cfargotunnel.com.\n"), "abc.cfargotunnel.com")

    def test_verdict_ok_when_points_to_cloudflare(self):
        with mock.patch.object(F, "resolve_cname", return_value="abc.cfargotunnel.com"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                bad = F.check_host("album.example.com")
        self.assertEqual(bad, 0)
        self.assertIn("已指向", buf.getvalue())

    def test_verdict_bad_when_wrong_target(self):
        with mock.patch.object(F, "resolve_cname", return_value="cdn.example.net"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                bad = F.check_host("album.example.com")
        self.assertEqual(bad, 1)
        self.assertIn("cfargotunnel.com", buf.getvalue())

    def test_verdict_bad_when_nothing_configured(self):
        with mock.patch.object(F, "resolve_cname", return_value=None):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                bad = F.check_host("album.example.com")
        self.assertEqual(bad, 1)
        self.assertIn("Public hostname", buf.getvalue())

    def test_no_host_skips_check(self):
        with mock.patch.object(F, "resolve_cname", side_effect=AssertionError("不该去查 DNS")):
            self.assertEqual(F.check_host(None), 0)


class TokenNeverLeaksTest(unittest.TestCase):
    """token 的可见性：命令行、日志、返回值都不该带着它。"""

    def test_command_line_has_no_token(self):
        cmd, _ = F.run_tunnel.__doc__, None
        # 真的去拼一次命令行（跑一个假的 cloudflared）
        with mock.patch.object(F, "spawn") as sp:
            sp.return_value = mock.MagicMock()
            sp.return_value.stderr = None
            sp.return_value.stdout = None
            F.run_tunnel(Path("cf.exe"), Path("cfg.yml"), {"TUNNEL_TOKEN": TOKEN}, timeout=0)
        args = sp.call_args[0][0]
        self.assertNotIn(TOKEN, args, "token 出现在命令行里会被任务管理器/ps 看到")
        self.assertIn("--config", args)
        self.assertEqual(sp.call_args[1]["env_extra"], {"TUNNEL_TOKEN": TOKEN})

    def test_setup_stores_token_in_env_and_never_echoes_it(self):
        """--setup 之后只允许 .env 里有 token，屏幕上不许回显。"""
        env_file = Path(tempfile.mkdtemp()) / ".env"
        # 注意要改 serve_common 的 ENV_PATH：write_env 用的是那一份常量，
        # 只改 serve_fixed 的等于没改 —— token 就会被写进真 .env
        # 两个 spawn 都要桩：spawn_app 走的是 serve_common 那份，只桩 serve_fixed 的
        # 会真的把 app.py 放出去（测试里起了个真进程，还会被 30 秒的 health 检查拖慢）
        with mock.patch.object(SC, "ENV_PATH", env_file), \
                mock.patch.object(F, "ensure_env"), \
                mock.patch.object(F, "check_host", return_value=0), \
                mock.patch.object(F, "write_config", return_value=Path("cfg.yml")), \
                mock.patch.object(F, "getpass", _fake_getpass(TOKEN)), \
                mock.patch.object(F, "input", lambda *a: "album.example.com"), \
                mock.patch.object(F, "find_cloudflared", return_value=Path("cf.exe")), \
                mock.patch.object(F, "spawn", mock.MagicMock()), \
                mock.patch.object(SC, "spawn", mock.MagicMock()), \
                mock.patch.object(F, "pick_port", return_value=5001), \
                mock.patch.object(F, "wait_health", return_value=True), \
                mock.patch.object(F, "run_tunnel", return_value=("x", _dead_tunnel_proc())), \
                mock.patch.object(sys, "argv", ["serve_fixed.py", "--setup"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                F.main()
        body = env_file.read_text(encoding="utf-8")
        self.assertIn("TUNNEL_TOKEN=" + TOKEN, body, "token 没落进 .env，下次跑就配不上了")
        self.assertIn("PUBLIC_HOST=album.example.com", body)
        self.assertNotIn(TOKEN, buf.getvalue(), "token 被打印到屏幕上了")

    def test_setup_never_touches_the_real_env(self):
        """桩点打错就会把假 token 写进仓库真 .env —— 这个坑真踩过。

        以前有版本只桩了 serve_fixed 的 ENV_PATH，而 write_env 读的是 serve_common
        那份常量；桩空一处，--setup 就把假 token 落进了仓库里的 .env，
        之后每次 `--check` 都以为自己配好了。这条用例就是那道闸。
        """
        real_env = ROOT / ".env"
        before = real_env.read_bytes() if real_env.exists() else b""
        sandbox = Path(tempfile.mkdtemp()) / ".env"
        sandbox.write_text("# 原有内容\n", encoding="utf-8")

        with mock.patch.object(SC, "ENV_PATH", sandbox), \
                mock.patch.object(F, "ensure_env"), \
                mock.patch.object(F, "check_host", return_value=0), \
                mock.patch.object(F, "write_config", return_value=Path("cfg.yml")), \
                mock.patch.object(F, "getpass", _fake_getpass(TOKEN)), \
                mock.patch.object(F, "input", lambda *a: "album.example.com"), \
                mock.patch.object(F, "find_cloudflared", return_value=Path("cf.exe")), \
                mock.patch.object(F, "pick_port", return_value=5001), \
                mock.patch.object(F, "wait_health", return_value=True), \
                mock.patch.object(F, "spawn", mock.MagicMock()), \
                mock.patch.object(SC, "spawn", mock.MagicMock()), \
                mock.patch.object(F, "run_tunnel", return_value=("x", _dead_tunnel_proc())), \
                mock.patch.object(sys, "argv", ["serve_fixed.py", "--setup"]):
            with contextlib.redirect_stdout(io.StringIO()):
                F.main()

        after = real_env.read_bytes() if real_env.exists() else b""
        self.assertEqual(before, after, "测试动到了仓库的 .env —— 有桩打错地方了")
        self.assertIn("TUNNEL_TOKEN=" + TOKEN, sandbox.read_text(encoding="utf-8"),
                      "token 应该落进临时那份 .env，而不是别处")


class MainFlowTest(unittest.TestCase):
    """跑通一次主流程，确认参数拼接与收尾都对。"""

    def _run(self, argv):
        """跑一次 main()，进程全部换成假的。

        返回 (退出码, 屏幕输出, 隧道进程的 spawn 调用, 相册进程的参数)。
        spawn_app 只替换成 recorder：它内部自己就把 BEHIND_TLS 拼进 env 了，
        从 spawn 那头是看不出来的（spawn 收到的是拼好的完整 env）。
        """
        sp = mock.MagicMock()
        sp.return_value.stderr = None       # run_tunnel 会读这两条，不能是 Mock 对象
        sp.return_value.stdout = None
        sp.return_value.poll.return_value = 0   # poll() 非 None，main 那句 while 才转一圈就出来
        app_calls: list[dict] = []

        def fake_spawn_app(port, host="127.0.0.1", env_extra=None):
            app_calls.append({"port": port, "host": host, "env_extra": env_extra or {}})
            return mock.MagicMock()

        with mock.patch.object(F, "spawn", sp), mock.patch.object(SC, "spawn", sp), \
                mock.patch.object(F, "spawn_app", side_effect=fake_spawn_app), \
                mock.patch.object(F, "ensure_env"), \
                mock.patch.object(F, "read_env", return_value={
                    "TUNNEL_TOKEN": TOKEN, "PUBLIC_HOST": "album.example.com"}), \
                mock.patch.object(F, "find_cloudflared", return_value=Path("cloudflared.exe")), \
                mock.patch.object(F, "check_host", return_value=0), \
                mock.patch.object(F, "wait_health", return_value=True), \
                mock.patch.object(F, "pick_port", return_value=5001), \
                mock.patch.object(F, "write_config", return_value=Path("cfg.yml")), \
                mock.patch.object(sys, "argv", argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = F.main()
            return rc, buf.getvalue(), sp, app_calls

    def test_prints_fixed_host(self):
        rc, out, _, _ = self._run(["serve_fixed.py"])
        self.assertEqual(rc, 0)
        self.assertIn("https://album.example.com", out)
        self.assertIn("固定地址", out)

    def test_does_not_echo_token(self):
        _, out, _, _ = self._run(["serve_fixed.py"])
        self.assertNotIn(TOKEN, out)

    def test_app_and_tunnel_both_spawned(self):
        """相册和隧道两个进程都得被拉起，只起一个说明流程断了。"""
        _, _, sp, app_calls = self._run(["serve_fixed.py"])
        self.assertEqual(len(app_calls), 1, "相册没被拉起，后面全白搭")
        self.assertEqual(sp.call_count, 1, "隧道没被拉起")

    def test_token_travels_in_env_only(self):
        """token 只准出现在隧道进程的 env 里，命令行一行都不许带。"""
        _, _, sp, app_calls = self._run(["serve_fixed.py"])
        for cmd, _kw in sp.call_args_list:
            self.assertNotIn(TOKEN, cmd, "token 进了命令行，任务管理器/ps 都能看到")
        self.assertTrue(app_calls, "没找到拉起相册的那次调用")
        self.assertNotIn("TUNNEL_TOKEN", app_calls[0]["env_extra"])

    def test_tunnel_process_gets_token(self):
        _, _, sp, _ = self._run(["serve_fixed.py"])
        cf_kw = [c[1] for c in sp.call_args_list if "cloudflared" in " ".join(c[0][0])]
        self.assertTrue(cf_kw, "没拉起 cloudflared")
        self.assertEqual(cf_kw[0].get("env_extra", {}).get("TUNNEL_TOKEN"), TOKEN,
                         "token 没交给隧道进程，隧道起不来")

    def test_app_runs_with_behind_tls(self):
        """固定域名这条是 https，相册必须按 BEHIND_TLS=1 起。"""
        _, _, _, app_calls = self._run(["serve_fixed.py"])
        self.assertTrue(app_calls, "没找到拉起相册的那次调用")
        self.assertEqual(app_calls[0]["env_extra"].get("BEHIND_TLS"), "1")
        self.assertEqual(app_calls[0]["host"], "127.0.0.1", "公网这条不能把相册绑到局域网网卡上")
        self.assertEqual(app_calls[0]["port"], 5001)


class SharedCloudflaredTest(unittest.TestCase):
    """两条路共用同一个 cloudflared 查找，别各找各的。"""

    def test_common_finds_by_env_and_public_reuses_it(self):
        import serve_public as SP

        tmp = Path(tempfile.mkdtemp())
        fake = tmp / "cloudflared.exe"
        fake.write_bytes(b"x")
        with mock.patch.dict(os.environ, {"CLOUDFLARED": str(fake)}):
            self.assertEqual(SC.find_cloudflared(), fake)
            self.assertEqual(SP.find_cloudflared(), fake,
                             "serve_public 还留着自己那份查找，改了一处另一处就找错")


if __name__ == "__main__":
    unittest.main()
