"""album 冒烟测试。跑：python -m unittest discover tests"""

import io
import os
import re
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="album-smoke-"))

# 必须在 import app 之前设置，否则 app 会拒绝启动
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["BEHIND_TLS"] = "0"
os.environ["UPLOAD_FOLDER"] = str(TMP)
os.environ["THUMB_FOLDER"] = str(TMP / "thumbs")   # 不设会写到仓库根目录
os.environ["DB_PATH"] = str(TMP / "album.db")

from argon2 import PasswordHasher  # noqa: E402

TEST_PWD = "correct horse battery staple"
os.environ["PASSWORD_HASH"] = PasswordHasher().hash(TEST_PWD)

sys.path.insert(0, str(ROOT))
import app as album  # noqa: E402
import store  # noqa: E402


# 从 app 里读真实配额，测试就不会因为调参而自己骗自己
LOGIN_LIMIT = int(re.split(r"[/\s]", album.LOGIN_RATE)[0])   # type: ignore[possibly-undefined]


def make_png(w=800, h=600, color=(120, 180, 240)) -> bytes:
    """生成一张真 PNG（不是随机字节），这样缩略图才会真的被生成。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        # 每个用例重置一次存储，否则用例之间会互相污染计数
        con = store.connect(os.environ["DB_PATH"])
        con.execute("DELETE FROM files")
        con.commit()
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # 把 WAL 并回主库
        except sqlite3.Error:
            pass
        con.close()
        for p in Path(TMP).glob("*"):
            if p.is_file() and p.suffix != ".db":
                try:
                    p.unlink()
                except OSError:
                    # WAL 模式下 -shm 可能被上一个用例残留的连接占着（Windows 上删不掉）。
                    # 不影响本用例：DELETE 是走 SQL 的，文件删不掉只会让下次重建 WAL。
                    pass
        thumbs = Path(TMP) / "thumbs"
        if thumbs.exists():
            for p in thumbs.glob("*"):
                p.unlink()

        album.limiter.enabled = False
        album.limiter.reset()
        self.app = album.app
        self.c = album.app.test_client()

    def login(self):
        self.c.post("/login", data={"password": TEST_PWD})
        with self.c.session_transaction() as s:
            return s.get("csrf_token", "")

    def upload_one(self, csrf: str, name: str, data: bytes):
        return self.c.post(
            "/upload",
            data={"files": (io.BytesIO(data), name), "csrf_token": csrf},
            content_type="multipart/form-data",
            follow_redirects=True,
        )


class StartupTest(unittest.TestCase):
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


class AuthTest(Base):
    def test_root_is_password_gate_not_upload_page(self):
        """根地址必须只有一个密码框，而不是上传页。"""
        body = self.c.get("/").get_data(as_text=True)
        self.assertIn("输入密码即可进入", body)
        self.assertIn('name="password"', body)

    def test_root_goes_straight_to_album_after_login(self):
        self.c.post("/login", data={"password": TEST_PWD})
        r = self.c.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/protected", r.headers["Location"])

    def test_open_redirect_blocked(self):
        r = self.c.post("/login?next=https://evil.example.com",
                        data={"password": TEST_PWD}, follow_redirects=False)
        loc = r.headers.get("Location", "")
        self.assertNotIn("evil.example.com", loc, "竟然跳到了站外地址")
        self.assertIn("/protected", loc)

    def test_rate_limit_page_is_human_readable(self):
        """限流命中时要给中文提示，否则用户会以为是自己密码错了。"""
        from app import too_many_requests
        with self.app.test_request_context("/"):
            html, code = too_many_requests(type("E", (), {"retry_after": 42})())
        self.assertEqual(code, 429)
        self.assertIn("尝试过于频繁", html)
        self.assertIn("这不是密码错误", html)   # 明确告诉用户这不是密码错

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

    def test_anonymous_upload_blocked(self):
        r = self.c.post("/upload", data={"csrf_token": "x"},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302, "未登录竟然能上传")
        self.assertIn("/login", r.headers["Location"])

    def test_media_requires_auth(self):
        fake_id = "a" * 32
        for url in (f"/thumb/{fake_id}", f"/preview/{fake_id}", f"/download/{fake_id}"):
            self.assertEqual(self.c.get(url).status_code, 302, f"未登录竟能访问 {url}")

    def test_security_headers(self):
        r = self.c.get("/")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")

    def test_login_rate_limited(self):
        album.limiter.enabled = True
        album.limiter.reset()
        codes = [self.c.post("/login", data={"password": "x"}).status_code
                 for _ in range(LOGIN_LIMIT + 3)]
        self.assertIn(429, codes, f"{LOGIN_LIMIT + 3} 次失败登录没触发限流: {codes}")
        album.limiter.enabled = False

    def test_rate_limit_is_post_only(self):
        """GET /login 不该被登录限流吃掉。"""
        album.limiter.enabled = True
        album.limiter.reset()
        codes = [self.c.get("/login").status_code for _ in range(LOGIN_LIMIT + 3)]
        self.assertNotIn(429, codes, f"GET 登录页被误限流: {codes}")
        album.limiter.enabled = False


class IdValidationTest(Base):
    """路由只接受 32 位十六进制 id，路径穿越在进文件系统之前就被挡掉。"""

    def test_bad_ids_rejected(self):
        self.login()
        for bad in ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "....//etc/passwd",
                    "abc", "A" * 32, "a" * 33]:
            for url in (f"/preview/{bad}", f"/download/{bad}", f"/thumb/{bad}"):
                self.assertEqual(self.c.get(url).status_code, 404,
                                 f"非法 id 没被挡住: {url}")


class UploadTest(Base):
    def test_chinese_filename_preserved(self):
        """磁盘用 UUID 存，真实中文名进数据库 —— secure_filename 不再吃掉它。"""
        csrf = self.login()
        self.upload_one(csrf, "旅行 相册(1).png", make_png())
        data = self.c.get("/api/files").get_json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["name"], "旅行 相册(1).png")

    def test_thumbnail_generated_and_smaller(self):
        csrf = self.login()
        self.upload_one(csrf, "big.png", make_png(2000, 1500))
        data = self.c.get("/api/files").get_json()
        item = data["items"][0]
        self.assertTrue(item["thumb"], "缩略图没生成")
        self.assertEqual(item["width"], 2000)
        self.assertEqual(item["height"], 1500)

        r = self.c.get(f"/thumb/{item['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("image/", r.headers["Content-Type"])
        self.assertLess(len(r.get_data()), 100_000, "缩略图竟然比原图还大")

    def test_dedup_by_content(self):
        """同样内容传两次：磁盘只存一份，数据库只留一条。"""
        csrf = self.login()
        payload = make_png(color=(10, 200, 30))
        self.upload_one(csrf, "a.png", payload)
        self.upload_one(csrf, "b.png", payload)   # 名字不同，内容相同

        data = self.c.get("/api/files").get_json()
        self.assertEqual(data["total"], 1, "相同内容没有去重")
        stored = list(Path(TMP).glob("*.png")) + list(Path(TMP).glob("*.tmp_*"))
        self.assertEqual(len(stored), 1, f"磁盘上存了 {len(stored)} 份，应该只有 1 份")

    def test_unsupported_type_rejected(self):
        csrf = self.login()
        before = self.c.get("/api/files").get_json()["total"]
        self.upload_one(csrf, "evil.svg", b"<svg onload=alert(1)>")
        self.assertEqual(self.c.get("/api/files").get_json()["total"], before)


class PagingTest(Base):
    def test_cursor_paging_no_dup_no_gap(self):
        csrf = self.login()
        for i in range(7):
            self.upload_one(csrf, f"img{i}.png", make_png(color=(i * 30 % 256, 80, 90)))

        seen, cursor = [], None
        while True:
            q = "/api/files?limit=3" + (f"&cursor={cursor}" if cursor else "")
            data = self.c.get(q).get_json()
            seen += [it["id"] for it in data["items"]]
            cursor = data["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(seen), 7, f"分页总数不对: {len(seen)}")
        self.assertEqual(len(set(seen)), 7, "分页出现重复项")

    def test_paging_when_timestamps_are_identical(self):
        """批量导入时 created_at 全都一样，游标必须仍然不重不漏。

        这条是回归测试：游标此前用 "%.6f" 编码，浮点进位会让上一页最后一条
        重新出现在下一页（列表偶发重复），并同时漏掉一条。
        """
        db_path = os.environ["DB_PATH"]
        con = store.connect(db_path)
        for i in range(6):
            store.insert_file(con, file_id="%032x" % (i + 1), orig_name=f"s{i}.png",
                              stored_name=f"s{i}.png", sha=f"sha{i}", size=1,
                              mime="image/png", width=1, height=1, thumb=0,
                              created_at=1700000000.0)   # 故意全部相同
        con.commit()
        con.close()   # 必须关掉，否则 Windows 上 -shm 文件还锁着，setUp 清理会失败

        seen, cursor = [], None
        while True:
            items, cursor, _ = store.list_files(store.connect(db_path), limit=2, cursor=cursor)
            seen += [it["id"] for it in items]
            if not cursor:
                break
        self.assertEqual(len(seen), 6, f"分页条数不对: {len(seen)}")
        self.assertEqual(len(set(seen)), 6, f"分页出现重复/漏项: {len(set(seen))}")

    def test_bad_cursor_falls_back_to_first_page(self):
        self.login()
        r = self.c.get("/api/files?cursor=not-a-real-cursor")
        self.assertEqual(r.status_code, 200)
        self.assertIn("items", r.get_json())


class DeleteTest(Base):
    def test_delete_removes_file_and_thumb(self):
        csrf = self.login()
        self.upload_one(csrf, "gone.png", make_png(1200, 900))
        item = self.c.get("/api/files").get_json()["items"][0]

        thumb_path = Path(TMP) / "thumbs" / store.thumb_name(item["id"])
        self.assertTrue(thumb_path.exists(), "缩略图文件不存在")

        r = self.c.post("/api/delete", json={"ids": [item["id"]]},
                        headers={"X-CSRFToken": csrf})
        self.assertTrue(r.get_json()["ok"])

        self.assertFalse(thumb_path.exists(), "缩略图没被删掉")
        self.assertEqual(self.c.get("/api/files").get_json()["total"], 0)

    def test_delete_requires_csrf(self):
        csrf = self.login()
        self.upload_one(csrf, "keep.png", make_png(600, 400))
        item = self.c.get("/api/files").get_json()["items"][0]
        r = self.c.post("/api/delete", json={"ids": [item["id"]]})
        self.assertEqual(r.status_code, 403, "缺 CSRF 竟然删成功了")


if __name__ == "__main__":
    unittest.main()
