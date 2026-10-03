"""生成演示图片，用于本地预览/验收。生产环境不要跑。

用法：python scripts/seed_demo.py [数量]
"""

import io
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as album  # noqa: E402

NAMES = [
    "旅行 相册(1).png", "海边日落.png", "猫咪睡觉.png", "毕业照.png",
    "春节 全家福.png", "深夜代码.png", "山顶云海.png", "街角咖啡.png",
    "樱花季.png", "老照片扫描.png", "朋友聚会.png", "清晨窗外.png",
]
COLORS = [
    (231, 76, 60), (52, 152, 219), (155, 89, 182), (46, 204, 113),
    (241, 196, 15), (230, 126, 34), (26, 188, 156), (149, 165, 166),
    (255, 105, 180), (100, 149, 237), (205, 133, 63), (106, 90, 205),
]


def make_png(i: int, w: int, h: int) -> bytes:
    im = Image.new("RGB", (w, h), COLORS[i % len(COLORS)])
    # 画点条纹，免得缩略图全是一片纯色看不出效果
    px = im.load()
    for y in range(0, h, max(1, h // 20)):
        for x in range(w):
            r, g, b = px[x, y]
            px[x, y] = (min(255, r + 40), min(255, g + 40), min(255, b + 40))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    c = album.app.test_client()
    album.limiter.enabled = False
    c.post("/login", data={"password": os.environ.get("DEMO_PASSWORD", "demo1234")})
    with c.session_transaction() as s:
        csrf = s.get("csrf_token", "")

    sizes = [(3000, 2000), (2000, 3000), (1600, 1200), (1200, 1600)]
    ok = 0
    for i in range(n):
        w, h = sizes[i % len(sizes)]
        name = NAMES[i % len(NAMES)] if i < len(NAMES) else f"演示图 {i+1}.png"
        r = c.post(
            "/upload",
            data={"files": (io.BytesIO(make_png(i, w, h)), name), "csrf_token": csrf},
            content_type="multipart/form-data",
        )
        if r.status_code in (200, 302):
            ok += 1
    print(f"已上传 {ok}/{n} 张演示图 → uploads/ + thumbs/")


if __name__ == "__main__":
    main()
