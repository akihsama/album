"""数据层：SQLite 元数据 + 缩略图 + 流式落盘。

为什么要有这一层：
1. 磁盘上只用 UUID 命名，真实文件名存数据库 —— 中文文件名不再被 secure_filename 吃掉。
2. 列表走游标分页，前端无限滚动，几千张也不会一次拉全量。
3. 落盘时顺手算 SHA-256，重复文件直接秒传（不占第二份空间）。
"""

import base64
import hashlib
import os
import sqlite3
import uuid
from datetime import datetime

# Pillow 是可选依赖：没装也能跑，只是不出缩略图（列表会退回原图）
try:
    from PIL import Image, ImageOps

    HAS_PIL = True
except ImportError:  # pragma: no cover
    HAS_PIL = False

THUMB_MAX = int(os.environ.get("THUMB_MAX", "480"))
THUMB_FORMAT = os.environ.get("THUMB_FORMAT", "webp").lower()
THUMB_QUALITY = int(os.environ.get("THUMB_QUALITY", "82"))

_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "txt": "text/plain",
    "pdf": "application/pdf",
    "zip": "application/zip",
    "csv": "text/csv",
}

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def is_image(name: str) -> bool:
    return ext_of(name) in IMAGE_EXTENSIONS


def mime_of(name: str) -> str:
    return _MIME.get(ext_of(name), "application/octet-stream")


# ----- 数据库 -----

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          TEXT PRIMARY KEY,
    orig_name   TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    sha256      TEXT,
    size        INTEGER NOT NULL,
    mime        TEXT,
    width       INTEGER,
    height      INTEGER,
    thumb       INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_cursor ON files(created_at DESC, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_sha
    ON files(sha256) WHERE sha256 IS NOT NULL;
"""


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")   # 读写不互相阻塞
    con.execute("PRAGMA synchronous=NORMAL")  # 单机自托管够用，别用 FULL（慢 10 倍）
    con.executescript(_SCHEMA)
    return con


def to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["orig_name"],
        "size": row["size"],
        "mime": row["mime"],
        "width": row["width"],
        "height": row["height"],
        "thumb": bool(row["thumb"]),
        "created_at": row["created_at"],
        "cursor": encode_cursor(row["created_at"], row["id"]),
    }


def encode_cursor(created_at: float, file_id: str) -> str:
    # 必须无损：用 repr(float) 而不是 "%.6f"。%.6f 会四舍五入，
    # 一旦游标值比真实 created_at 大，上一页最后一条就会重新出现在下一页
    # —— 表现为列表偶发重复一条（并同时漏一条），且只在浮点恰好进位时发生。
    raw = f"{float(created_at)!r}|{file_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str):
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + pad).decode()
        ts, file_id = raw.split("|", 1)
        return float(ts), file_id
    except Exception:
        return None


def list_files(con, limit: int = 60, cursor: str | None = None) -> tuple[list, str | None, int]:
    """游标分页。用 (created_at, id) 而不是 OFFSET —— OFFSET 5000 要先扫过 5000 行再丢掉。"""
    args: list = []
    where = ""
    if cursor:
        cur = decode_cursor(cursor)
        if cur:
            where = "WHERE (created_at < ?) OR (created_at = ? AND id < ?)"
            args = [cur[0], cur[0], cur[1]]

    rows = con.execute(
        f"SELECT * FROM files {where} ORDER BY created_at DESC, id DESC LIMIT ?",
        (*args, limit + 1),  # 多取一条用来判断"还有没有下一页"
    ).fetchall()

    items = [to_dict(r) for r in rows[:limit]]
    # 游标必须指向"本页最后一条"，而不是"本页后第一条"。
    # 指向后一条的话，WHERE 会把它自己也排除掉 —— 结果永远漏掉一张图
    # （多张图 created_at 相同时（秒传/批量导入），漏的正好是这一页最后一张）。
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_cursor(last["created_at"], last["id"])
    else:
        next_cursor = None
    total = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    return items, next_cursor, total


def get_file(con, file_id: str):
    return con.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()


def find_by_sha(con, sha: str):
    return con.execute("SELECT * FROM files WHERE sha256 = ?", (sha,)).fetchone()


def insert_file(con, *, file_id, orig_name, stored_name, sha, size, mime, width, height, thumb, created_at):
    con.execute(
        "INSERT INTO files (id, orig_name, stored_name, sha256, size, mime, width, height, thumb, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (file_id, orig_name, stored_name, sha, size, mime, width, height, int(thumb), created_at),
    )
    con.commit()


def delete_file(con, file_id: str):
    con.execute("DELETE FROM files WHERE id = ?", (file_id,))
    con.commit()


# ----- 落盘 -----

def save_stream(stream, dest_path: str, chunk: int = 1 << 20) -> tuple[int, str]:
    """流式写盘 + 边写边算 SHA-256。

    不要 stream.read() 一次读全文件：几张大图并发就能把内存吃光。
    """
    h = hashlib.sha256()
    size = 0
    with open(dest_path, "wb") as f:
        while True:
            buf = stream.read(chunk)
            if not buf:
                break
            h.update(buf)
            size += len(buf)
            f.write(buf)
    return size, h.hexdigest()


def make_thumbnail(src: str, dest: str) -> tuple[int, int] | None:
    """生成缩略图，返回 (width, height)；失败返回 None（不阻断上传）。"""
    if not HAS_PIL:
        return None
    try:
        with Image.open(src) as im:
            # 手机竖拍照片的方向在 EXIF 里，不转的话相册里全是躺着的图
            im = ImageOps.exif_transpose(im)
            w, h = im.size
            im.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
            tw, th = im.size

            if im.mode in ("RGBA", "LA", "P"):
                # 保留透明通道（PNG 截图带 alpha 很常见）
                im = im.convert("RGBA")
            elif im.mode != "RGB":
                im = im.convert("RGB")

            if THUMB_FORMAT == "webp":
                im.save(dest, "WEBP", quality=THUMB_QUALITY, method=4)
            elif THUMB_FORMAT == "jpeg":
                im.save(dest, "JPEG", quality=THUMB_QUALITY, optimize=True)
            else:
                im.save(dest, "PNG", optimize=True)
            return w, h
    except Exception:
        # 缩略图失败不应该让上传失败：列表会退回显示原图
        return None


def thumb_name(file_id: str) -> str:
    ext = "webp" if THUMB_FORMAT == "webp" else ("jpg" if THUMB_FORMAT == "jpeg" else "png")
    return f"{file_id}.{ext}"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def new_id() -> str:
    return uuid.uuid4().hex


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
