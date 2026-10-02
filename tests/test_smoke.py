"""album 冒烟测试。跑：python -m unittest discover tests"""

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="album-smoke-"))

# 必须在 import app 之前设置，否则 app 会拒绝启动
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["PASSWORD_HASH"] = None or ""
os.environ["UPLOAD_FOLDER"] = str(TMP)
os.environ["BEHIND_TLS"] = "0"

from argon2 import PasswordHasher

TEST_PWD = "correct horse battery staple"
os.environ["PASSWORD_HASH"] = PasswordHasher().hash(TEST_PWD)

sys.path.insert(0, str(ROOT))
import app as album  # noqa: E402


class SmokeTest(unittest.TestCase):
    def setUp(self):
        album.limiter.enabled = False
        album.limiter.reset()
        self.c = album.app.test_client()

    # ---------- 启动前校验 ----------

    def test_missing_secret_key_refuses_start(self):
        env = dict(os.environ, SECRET_KEY="")
        r = subprocess.run([sys.executable, "-c", "import app"], cwd=ROOT,
                           env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "缺 SECRET_KEY 竟然启动成功了")
        self.assertIn("SECRET_KEY", r.stderr + r.stdout)

    def test_missing_password_hash_refuses_start(self):
        env = dict(os.environ, PASSWORD_HASH="")
        r = subprocess.run([sys.executable, "-c", "import app"], cwd=ROOT,
                           env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "缺 PASSWORD_HASH 竟然启动成功了")

    # ---------- 认证 ----------

    def test_index_is_public(self):
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_protected_requires_login(self):
        r = self.c.get("/protected")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_wrong_password_rejected(self):
        r = self.c.post("/login", data={"password": "nope"}, follow_redirects=True)
        self.assertIn("密码错误", r.get_data(as_text=True))

    def test_correct_password_works(self):
        r = self.c.post("/login", data={"password": TEST_PWD})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/protected").status_code, 200)

    def test_logout_clears_session(self):
        self.c.post("/login", data={"password": TEST_PWD})
        self.c.get("/logout")
        self.assertEqual(self.c.get("/protected").status_code, 302)

    def test_preview_requires_auth(self):
        p = TMP / "abc_test.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        r = self.c.get(f"/preview/{p.name}")
        self.assertEqual(r.status_code, 302, "未登录竟然能预览")

    # ---------- 路径穿越 ----------

    def test_path_traversal_blocked(self):
        self.c.post("/login", data={"password": TEST_PWD})
        for bad in ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "....//etc/passwd"]:
            self.assertEqual(self.c.get(f"/preview/{bad}").status_code, 404,
                             f"穿越 payload 没被挡住: {bad}")

    # ---------- 安全响应头 ----------

    def test_security_headers(self):
        r = self.c.get("/")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")

    # ---------- 限流 ----------

    def test_login_rate_limited(self):
        album.limiter.enabled = True
        album.limiter.reset()
        codes = [self.c.post("/login", data={"password": "x"}).status_code
                 for _ in range(13)]
        self.assertIn(429, codes, f"13 次失败登录没触发限流: {codes}")

    def test_rate_limit_is_post_only(self):
        """GET /login 不该被登录限流吃掉。"""
        album.limiter.enabled = True
        album.limiter.reset()
        codes = [self.c.get("/login").status_code for _ in range(13)]
        self.assertNotIn(429, codes, f"GET 登录页被误限流: {codes}")
        album.limiter.enabled = False


if __name__ == "__main__":
    unittest.main()
