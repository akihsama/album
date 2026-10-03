"""局域网 / 公网两条启动路径 + 原子落盘的回归测试。

重点守三件事：
  1. 两条路互不干涉（端口各自独立、进程各自清理、都能单独跑）；
  2. 启动前的检查能把「app.py 一启动就 SystemExit」的坑挡在门外；
  3. 落盘是原子的 —— 两个实例并跑时谁都读不到半截文件。
"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import store  # noqa: E402
import serve_common as SC  # noqa: E402
import serve_public as SP  # noqa: E402


class EnvFileTest(unittest.TestCase):
    """serve_common 的 .env 读写：别把用户注释和现有配置重写没了。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.f = Path(self.dir) / ".env"
        self.f.write_text("# 我的注释\nSECRET_KEY=abc\nBEHIND_TLS=0\n", encoding="utf-8")
        self.patcher = mock.patch.object(SC, "ENV_PATH", self.f)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_read_env_strips_quotes_and_skips_comments(self):
        env = SC.read_env()
        self.assertEqual(env["SECRET_KEY"], "abc")
        self.assertEqual(env["BEHIND_TLS"], "0")
        self.assertNotIn("我的注释", env)

    def test_write_env_keeps_others_intact(self):
        SC.write_env({"PASSWORD_HASH": "$argon2id$x"})
        body = self.f.read_text(encoding="utf-8")
        self.assertIn("# 我的注释", body)
        self.assertIn("BEHIND_TLS=0", body, "不该把没动过的键弄丢")
        self.assertIn("PASSWORD_HASH=$argon2id$x", body)

    def test_write_env_appends_when_key_missing(self):
        SC.write_env({"SECRET_KEY": "new"})
        self.assertEqual(SC.read_env()["SECRET_KEY"], "new")


class PortSelectionTest(unittest.TestCase):
    """两个脚本要能各自独立选到端口，不能撞车。"""

    def test_port_busy_detects_occupied_port(self):
        s = __import__("socket").socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            self.assertTrue(SC.port_busy(port), "占用中的端口没被检测出来")
        finally:
            s.close()
        self.assertFalse(SC.port_busy(port), "刚关掉的端口还被认为占用")

    def test_pick_port_skips_busy_one(self):
        """真有一整段都被占时，应顺延到空位（socket 保持占用状态）。"""
        held = []
        try:
            for _ in range(2):
                s = __import__("socket").socket()
                s.bind(("127.0.0.1", 0))
                s.listen(1)
                held.append(s)
            first = held[0].getsockname()[1]
            # 内核给端口是随机跳的，别断言"恰好 +1"，只断言它确实躲开了占用的那个
            chosen = SC.pick_port(first, first)
            self.assertNotEqual(chosen, first, "被占的端口没被跳过")
            self.assertFalse(SC.port_busy(chosen), "挑出来的端口居然还是占用状态")
        finally:
            for s in held:
                s.close()

    def test_default_ports_do_not_collide(self):
        """局域网 5000 / 公网 5001 —— 这是「互不干涉」的第一道保险。"""
        self.assertNotEqual(5000, SP.DEFAULT_PORT)
        self.assertEqual(SP.DEFAULT_PORT, 5001)


class TunnelUrlTest(unittest.TestCase):
    """从 cloudflared 的输出里抓地址：真输出长这样才算对。"""

    cases = [
        "+0000 2026-10-03T10:00:00Z INF https://things-have-faces-record.trycloudflare.com "
        "is registered in DNS, now attempting to connect in 2s\n",
        "INF Starting Hello World server\n+0000 INF https://a-b-c-d.trycloudflare.com\n",
    ]

    def test_parses_real_tunnel_output(self):
        for line in self.cases:
            m = SP.CF_URL_RE.search(line)
            self.assertIsNotNone(m, f"没从这行里抓到地址：{line}")
            self.assertTrue(m.group(0).startswith("https://"))
            self.assertTrue(m.group(0).endswith("trycloudflare.com"))

    def test_ignores_non_tunnel_urls(self):
        self.assertIsNone(SP.CF_URL_RE.search("http://127.0.0.1:5001"))
        self.assertIsNone(SP.CF_URL_RE.search("https://example.com"))


class AtomicWriteTest(unittest.TestCase):
    """落盘原子性：两个实例并跑时不能互相截断文件。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def find_tmp(self) -> list[str]:
        return [p.name for p in Path(self.dir).iterdir() if p.name.endswith(".tmp")]

    def test_save_stream_creates_final_file_without_tmp_leftover(self):
        dest = Path(self.dir) / "a.bin"
        size, sha = store.save_stream(io.BytesIO(b"hello" * 4), str(dest))
        self.assertEqual(size, 20)
        self.assertEqual(sha, __import__("hashlib").sha256(b"hello" * 4).hexdigest())
        self.assertEqual(dest.read_bytes(), b"hello" * 4)
        self.assertEqual(self.find_tmp(), [], "临时文件没清干净")

    def test_save_stream_overwrites_existing_file_atomically(self):
        dest = Path(self.dir) / "b.bin"
        dest.write_bytes(b"old-old-old-old-old-old-old")
        store.save_stream(io.BytesIO(b"new"), str(dest))
        self.assertEqual(dest.read_bytes(), b"new")
        self.assertEqual(self.find_tmp(), [])

    def test_make_thumbnail_writes_without_tmp_leftover(self):
        from PIL import Image

        src = Path(self.dir) / "in.png"
        Image.new("RGB", (2000, 1200), (30, 120, 200)).save(src)
        dest = str(Path(self.dir) / "in.thumb")
        res = store.make_thumbnail(str(src), dest)
        self.assertIsNotNone(res, "缩略图生成失败")
        self.assertTrue(os.path.exists(dest))
        self.assertEqual(self.find_tmp(), [], "缩略图临时文件残留")


class TlsSemanticsTest(unittest.TestCase):
    """公网必须按 https 起，局域网按 http 起 —— 弄反了就是公网裸奔。"""

    def test_public_path_declares_behind_tls(self):
        self.assertEqual(SP.PUBLIC_TLS_ENV.get("BEHIND_TLS"), "1",
                         "公网那条路没声明 BEHIND_TLS=1，会话 cookie 会缺 Secure 标记")

    def test_spawn_app_defaults_to_http(self):
        """局域网是 http，默认不能强制 Secure（否则浏览器不存 cookie）。"""
        # 只验配置构造，不起真进程
        import inspect

        src = inspect.getsource(SC.spawn_app)
        self.assertIn('"BEHIND_TLS": "0"', src)


class SpawnPipeTest(unittest.TestCase):
    """cloudflared 要读输出就得开管道 —— 漏了这个参数会直接 AttributeError。"""

    def test_pipe_mode_opens_streams(self):
        import serve_common as SC_mod
        p = SC_mod.spawn(["python", "-c", "print('hi')"], pipe=True)
        try:
            self.assertIsNotNone(p.stdout, "pipe 模式下 stdout 必须是可读流")
            self.assertIsNotNone(p.stderr, "pipe 模式下 stderr 必须是可读流")
            self.assertEqual(p.stdout.readline().strip(), "hi")
        finally:
            p.stdout.close()
            p.stdout = None
            p.stderr = None
            p.wait(timeout=10)
            SC_mod.stop(p)

    def test_default_mode_has_no_streams(self):
        """不开 pipe 时仍是继承输出，别把默认行为改坏了。"""
        p = SC.spawn(["python", "-c", "pass"])
        self.assertIsNone(p.stdout)
        p.wait(timeout=10)
        SC.stop(p)


class FindCloudflaredTest(unittest.TestCase):
    """cloudflared 找不到时脚本要去下载，所以查找逻辑必须干净可预测。"""

    def test_honors_explicit_env_var_when_present(self):
        """CLOUDFLARED 指到真文件上，就得认它，不跑去下载。"""
        tmp = Path(tempfile.mkdtemp())
        fake = tmp / "cloudflared.exe"
        fake.write_bytes(b"x")
        with mock.patch.dict(os.environ, {"CLOUDFLARED": str(fake)}):
            self.assertEqual(SP.find_cloudflared(), fake)

    def test_ignores_env_var_pointing_to_missing_file(self):
        """指向不存在的文件 ≠ 找到了；候选目录也空时老实返回 None（让脚本去下载）。"""
        empty = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"CLOUDFLARED": str(empty / "no-such.exe")}), \
                mock.patch.object(SP.shutil, "which", return_value=None), \
                mock.patch.object(SP, "ROOT", empty):
            self.assertIsNone(SP.find_cloudflared())


if __name__ == "__main__":
    unittest.main()
