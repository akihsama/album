import logging
import os
import threading
import uuid
import secrets
from datetime import timedelta
from functools import wraps

from flask import (
    Flask,
    g,
    jsonify,
    request,
    redirect,
    url_for,
    render_template,
    flash,
    send_file,
    session,
    abort,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("album")


def _load_dotenv():
    """零依赖读取 .env：只填充尚未设置的环境变量，已存在的（含容器注入）优先。"""
    path = os.environ.get("ENV_FILE", ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()


# ----- 配置：关键项缺失就拒绝启动，绝不静默降级 -----
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
THUMB_FOLDER = os.environ.get("THUMB_FOLDER", "thumbs")
DB_PATH = os.environ.get("DB_PATH", os.path.join(UPLOAD_FOLDER, "album.db"))
ALLOWED_EXTENSIONS = {"txt", "pdf", "png", "jpg", "jpeg", "gif", "webp", "zip", "csv"}
# 默认与 nginx 的 client_max_body_size 50M 对齐，避免"收完了才被拒"
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 50 * 1024 * 1024))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "60"))
# 私人云盘默认不允许匿名上传；要恢复旧行为设 ALLOW_ANONYMOUS_UPLOAD=1
ALLOW_ANONYMOUS_UPLOAD = os.environ.get("ALLOW_ANONYMOUS_UPLOAD", "0") == "1"

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise SystemExit("必须设置 SECRET_KEY（生成：openssl rand -hex 32）")

PASSWORD_HASH = os.environ.get("PASSWORD_HASH")
if not PASSWORD_HASH:
    raise SystemExit("必须设置 PASSWORD_HASH（生成：python scripts/init_password.py）")

# 反向代理已终结 TLS 时为 1；本地 http 调试设 BEHIND_TLS=0
BEHIND_TLS = os.environ.get("BEHIND_TLS", "1") == "1"
SESSION_LIFETIME_DAYS = int(os.environ.get("SESSION_LIFETIME_DAYS", "7"))

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["THUMB_FOLDER"] = THUMB_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = SECRET_KEY

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=SESSION_LIFETIME_DAYS)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = BEHIND_TLS   # 只在 TLS 下发送 cookie

# 让 Flask 识别真实协议与来源 IP（否则限流拿到的全是 nginx 的内网 IP）
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# 注意：memory:// 在多 worker 下是"每进程各算一份"，4 worker 等于配额放大 4 倍。
# 单用户场景够用；要严格限流改成 storage_uri="redis://redis:6379"
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["600/hour"],
    storage_uri=os.environ.get("LIMITER_STORAGE", "memory://"),
)

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["THUMB_FOLDER"], exist_ok=True)

_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

# 限制同时生成缩略图的线程数：4000x3000 解码后 ~36MB RGB，加上 resize 缓冲，
# 单张峰值能到几百 MB。小内存机器上并发几张就会撑爆。
_thumb_sem = threading.Semaphore(int(os.environ.get("THUMB_CONCURRENCY", "2")))

# 登录限流：单用户自用，20/分钟足够挡住脚本爆破，又不会自己调试时把自己锁住。
# 触发后给的是友好页面（见 errorhandler 429），而不是 Werkzeug 的裸 429 文本，
# 否则用户会以为"密码不对"。
LOGIN_RATE = os.environ.get("LOGIN_RATE", "20/minute")


# ----- 数据库（每请求一连接）-----
def get_db():
    if "db" not in g:
        g.db = store.connect(DB_PATH)
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ----- 工具函数 -----
def verify_password(pwd: str) -> bool:
    """Argon2id 校验。慢哈希，抗 GPU/ASIC 爆破。"""
    try:
        return _ph.verify(PASSWORD_HASH, pwd)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def allowed_file(filename):
    return "." in filename and store.ext_of(filename) in ALLOWED_EXTENSIONS


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return decorated


def ensure_csrf():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(16)
    return session["csrf_token"]


def check_csrf_json() -> bool:
    """fetch 请求从 header 取 token，避免把 token 拼在 URL 里。"""
    token = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
    return bool(token) and token == session.get("csrf_token")


def _safe_id(file_id: str) -> bool:
    return len(file_id) == 32 and all(c in "0123456789abcdef" for c in file_id)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


# ----- 上传落库 -----
def ingest(file) -> tuple[str, str]:
    """返回 (状态, 展示名)。状态：saved / dup / skipped / failed"""
    orig = os.path.basename(file.filename or "")
    if not orig or not allowed_file(orig):
        return "skipped", orig

    ext = store.ext_of(orig)
    tmp = os.path.join(app.config["UPLOAD_FOLDER"], f".tmp_{uuid.uuid4().hex}")
    try:
        size, sha = store.save_stream(file.stream, tmp)
    except Exception as e:
        log.warning("写入失败 %s: %s", orig, e)
        if os.path.exists(tmp):
            os.remove(tmp)
        return "failed", orig

    con = get_db()
    hit = store.find_by_sha(con, sha)
    if hit:
        # 秒传：内容完全一致，不占第二份磁盘
        os.remove(tmp)
        log.info("秒传命中 %s -> %s", orig, hit["id"])
        return "dup", orig

    file_id = store.new_id()
    stored = f"{file_id}.{ext}"
    final = os.path.join(app.config["UPLOAD_FOLDER"], stored)
    os.replace(tmp, final)

    width = height = None
    has_thumb = 0
    if store.is_image(orig):
        with _thumb_sem:
            dims = store.make_thumbnail(final, os.path.join(app.config["THUMB_FOLDER"], store.thumb_name(file_id)))
        if dims:
            width, height = dims
            has_thumb = 1
        else:
            log.warning("缩略图生成失败 %s", orig)

    store.insert_file(
        con,
        file_id=file_id,
        orig_name=orig,
        stored_name=stored,
        sha=sha,
        size=size,
        mime=store.mime_of(orig),
        width=width,
        height=height,
        thumb=has_thumb,
        created_at=os.path.getmtime(final),
    )
    return "saved", orig


# ----- 路由 -----


def safe_next(raw: str) -> str:
    """只接受站内绝对路径，挡掉 ?next=https://evil.com 这类开放重定向。"""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("protected")


# 根地址就是唯一的门：已登录 → 直接进相册；未登录 → 只要求输一次密码
@app.route("/", methods=["GET"])
def gate():
    if session.get("auth"):
        return redirect(url_for("protected"))
    return render_template("login.html", next=url_for("protected"))


@app.route("/upload", methods=["GET"])
def upload_page():
    if not ALLOW_ANONYMOUS_UPLOAD and not session.get("auth"):
        return redirect(url_for("login", next=url_for("upload_page")))
    csrf = ensure_csrf()
    return render_template("index.html", allowed=", ".join(sorted(ALLOWED_EXTENSIONS)),
                           csrf=csrf)


@app.route("/upload", methods=["POST"])
@limiter.limit("60/minute")
def upload():
    if not ALLOW_ANONYMOUS_UPLOAD and not session.get("auth"):
        return redirect(url_for("login", next=request.path))
    form_csrf = request.form.get("csrf_token")
    if not form_csrf or form_csrf != session.get("csrf_token"):
        flash("无效的请求（CSRF 检测失败）", "error")
        return redirect(url_for("upload_page"))

    files = request.files.getlist("files")
    if not files:
        flash("未选择文件", "error")
        return redirect(url_for("upload_page"))

    saved, dup, skipped, failed = [], [], [], []
    for file in files:
        if not file or not file.filename:
            continue
        status, name = ingest(file)
        {"saved": saved, "dup": dup, "skipped": skipped, "failed": failed}[status].append(name)

    if saved:
        flash(f"已上传 {len(saved)} 个：{', '.join(saved[:5])}{'…' if len(saved) > 5 else ''}", "success")
    if dup:
        flash(f"{len(dup)} 个内容重复，已秒传不占空间：{', '.join(dup[:5])}", "info")
    if skipped:
        flash(f"{len(skipped)} 个被跳过（不支持的类型）", "error")
    if failed:
        flash(f"{len(failed)} 个写入失败，请查看日志", "error")
    return redirect(url_for("protected"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE, methods=["POST"])
def login():
    next_url = safe_next(request.args.get("next"))
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if verify_password(pwd):
            # 登录成功必须重建会话，防会话固定
            session.clear()
            session["auth"] = True
            session.permanent = True
            ensure_csrf()
            log.info("登录成功 ip=%s", request.remote_addr)
            flash("登录成功", "success")
            return redirect(next_url)
        log.warning("登录失败 ip=%s", request.remote_addr)
        flash("密码错误", "error")
        return render_template("login.html", next=next_url)
    return render_template("login.html", next=next_url)


@app.route("/logout")
def logout():
    session.clear()
    flash("已登出", "info")
    return redirect(url_for("upload_page"))


@app.route("/protected")
@login_required
def protected():
    ensure_csrf()
    total = get_db().execute("SELECT COUNT(*) FROM files").fetchone()[0]
    return render_template("protected.html", total=total, page_size=PAGE_SIZE,
                           csrf=session["csrf_token"], has_pil=store.HAS_PIL)


@app.route("/api/files")
@login_required
def api_files():
    """游标分页。第一屏随页面渲染，后续由前端无限滚动拉取。"""
    try:
        limit = min(int(request.args.get("limit", PAGE_SIZE)), 200)
    except ValueError:
        limit = PAGE_SIZE
    items, next_cursor, total = store.list_files(get_db(), limit=limit, cursor=request.args.get("cursor"))
    return jsonify({"items": items, "next_cursor": next_cursor, "total": total})


@app.route("/thumb/<file_id>")
@login_required
def thumb(file_id):
    if not _safe_id(file_id):
        abort(404)
    row = store.get_file(get_db(), file_id)
    if not row or not row["thumb"]:
        abort(404)
    path = os.path.join(app.config["THUMB_FOLDER"], store.thumb_name(file_id))
    if not os.path.exists(path):
        abort(404)
    # 缩略图是内容寻址的静态产物，可以放心缓存
    return send_file(path, mimetype=f"image/{store.THUMB_FORMAT}", max_age=86400)


@app.route("/preview/<file_id>")
@login_required
def preview(file_id):
    if not _safe_id(file_id):
        abort(404)
    row = store.get_file(get_db(), file_id)
    if not row or not store.is_image(row["orig_name"]):
        abort(404)
    return send_file(os.path.join(app.config["UPLOAD_FOLDER"], row["stored_name"]),
                     mimetype=row["mime"])


@app.route("/download/<file_id>")
@login_required
def download(file_id):
    if not _safe_id(file_id):
        abort(404)
    row = store.get_file(get_db(), file_id)
    if not row:
        abort(404)
    # download_name 保留原始中文名，Flask 会按 RFC 5987 编码
    return send_file(os.path.join(app.config["UPLOAD_FOLDER"], row["stored_name"]),
                     mimetype=row["mime"], as_attachment=True, download_name=row["orig_name"])


@app.route("/api/delete", methods=["POST"])
@login_required
def api_delete():
    if not check_csrf_json():
        return jsonify({"ok": False, "error": "CSRF 校验失败"}), 403

    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids") or ([payload["id"]] if payload.get("id") else [])
    if not ids:
        return jsonify({"ok": False, "error": "未指定文件"}), 400

    con = get_db()
    removed = []
    for file_id in ids:
        if not _safe_id(file_id):
            continue
        row = store.get_file(con, file_id)
        if not row:
            continue
        for p in (
            os.path.join(app.config["UPLOAD_FOLDER"], row["stored_name"]),
            os.path.join(app.config["THUMB_FOLDER"], store.thumb_name(file_id)),
        ):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError as e:
                log.warning("删除失败 %s: %s", p, e)
        store.delete_file(con, file_id)
        removed.append(file_id)

    log.info("删除 %d 个文件", len(removed))
    return jsonify({"ok": True, "removed": removed})


@app.errorhandler(413)
def request_entity_too_large(error):
    flash(f"上传文件超出限制（最大 {app.config['MAX_CONTENT_LENGTH']} 字节）", "error")
    return redirect(url_for("upload_page")), 413


@app.errorhandler(429)
def too_many_requests(error):
    """限流命中时给中文提示，别让用户以为是自己密码输错了。"""
    retry = getattr(error, "retry_after", None) or 60
    return render_template("ratelimited.html", retry_after=int(retry)), 429


if __name__ == "__main__":
    # 仅本地调试。生产一律走 gunicorn（见 Dockerfile）。
    # 绝不要开 debug=True：Werkzeug 调试器带交互式控制台，等于把 shell 挂出去。
    app.run(host="127.0.0.1", port=5000, debug=False)
