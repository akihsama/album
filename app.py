import logging
import os
import uuid
import secrets
from datetime import timedelta
from functools import wraps

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    render_template,
    flash,
    send_from_directory,
    session,
    abort,
)
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("album")


# ----- 配置：关键项缺失就拒绝启动，绝不静默降级 -----
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
ALLOWED_EXTENSIONS = {"txt", "pdf", "png", "jpg", "jpeg", "gif", "zip", "csv"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}
# 默认与 nginx 的 client_max_body_size 50M 对齐，避免"收完了才被拒"
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 50 * 1024 * 1024))

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

_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


# ----- 工具函数 -----
def verify_password(pwd: str) -> bool:
    """Argon2id 校验。慢哈希，抗 GPU/ASIC 爆破。"""
    try:
        return _ph.verify(PASSWORD_HASH, pwd)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def is_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in IMAGE_EXTENSIONS


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


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


# ----- 路由 -----
@app.route("/", methods=["GET"])
def index():
    csrf = ensure_csrf()
    return render_template("index.html", allowed=", ".join(sorted(ALLOWED_EXTENSIONS)), csrf=csrf)


@app.route("/upload", methods=["POST"])
@limiter.limit("60/minute")
def upload():
    # Optional CSRF check even for anonymous uploads
    form_csrf = request.form.get("csrf_token")
    if not form_csrf or form_csrf != session.get("csrf_token"):
        flash("无效的请求（CSRF 检测失败）", "error")
        return redirect(url_for("index"))

    if "files" not in request.files:
        flash("未检测到文件字段", "error")
        return redirect(url_for("index"))

    files = request.files.getlist("files")
    if not files:
        flash("未选择文件", "error")
        return redirect(url_for("index"))

    saved = []
    for file in files:
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            unique_name = f"{uuid.uuid4().hex}_{filename}"
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
            file.save(save_path)
            saved.append(filename)
    if saved:
        flash(f"已上传: {', '.join(saved)}", "success")
    else:
        flash("没有可上传的文件（可能是不支持的类型）", "error")
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
def login():
    next_url = request.args.get("next") or url_for("protected")
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
    return redirect(url_for("index"))


@app.route("/protected")
@login_required
def protected():
    ensure_csrf()
    raw_files = sorted(os.listdir(app.config["UPLOAD_FOLDER"]))
    files = []
    for fn in raw_files:
        display = fn
        if "_" in fn:
            # display original name after the first underscore
            parts = fn.split("_", 1)
            display = parts[1]
        files.append({"saved_name": fn, "display_name": display, "is_image": is_image(fn)})
    return render_template("protected.html", files=files)


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    # Only allow filenames that are exact matches in the upload folder
    safe_name = secure_filename(filename)
    if safe_name != filename:
        abort(404)
    return send_from_directory(app.config["UPLOAD_FOLDER"], safe_name, as_attachment=True)


@app.route("/preview/<path:filename>")
@login_required
def preview(filename):
    # only preview image types
    safe_name = secure_filename(filename)
    if safe_name != filename or not is_image(filename):
        abort(404)
    # send file to be displayed inline by the browser
    return send_from_directory(app.config["UPLOAD_FOLDER"], safe_name)


@app.route("/delete", methods=["POST"])
@login_required
def delete_file():
    form_csrf = request.form.get("csrf_token")
    if not form_csrf or form_csrf != session.get("csrf_token"):
        flash("无效的请求（CSRF 检测失败）", "error")
        return redirect(url_for("protected"))

    filename = request.form.get("filename")
    if not filename:
        flash("未指定要删除的文件", "error")
        return redirect(url_for("protected"))

    safe_name = secure_filename(filename)
    if safe_name != filename:
        flash("无效的文件名", "error")
        return redirect(url_for("protected"))

    path = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)
    if not os.path.exists(path):
        flash("文件不存在", "error")
        return redirect(url_for("protected"))

    try:
        os.remove(path)
        flash(f"已删除: {safe_name}", "success")
    except Exception as e:
        flash(f"删除失败: {e}", "error")

    return redirect(url_for("protected"))


@app.errorhandler(413)
def request_entity_too_large(error):
    flash(f"上传文件超出限制（最大 {app.config['MAX_CONTENT_LENGTH']} 字节）", "error")
    return redirect(url_for("index")), 413


if __name__ == "__main__":
    # 仅本地调试。生产一律走 gunicorn（见 Dockerfile）。
    # 绝不要开 debug=True：Werkzeug 调试器带交互式控制台，等于把 shell 挂出去。
    app.run(host="127.0.0.1", port=5000, debug=False)
