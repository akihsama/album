import os
import uuid
import secrets
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
    safe_join,
)
from werkzeug.utils import secure_filename

# ----- 配置 -----
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
ALLOWED_EXTENSIONS = {"txt", "pdf", "png", "jpg", "jpeg", "gif", "zip", "csv"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024))
PASSWORD = os.environ.get("PASSWORD", "changeme")
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_urlsafe(32))
ENABLE_SSL = os.environ.get("ENABLE_SSL", "0") == "1"

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = SECRET_KEY
# Security-related cookie settings (effective when behind TLS)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True  # requires TLS in production

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


# ----- 工具函数 -----
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


# ----- 路由 -----
@app.route("/", methods=["GET"])
def index():
    csrf = ensure_csrf()
    return render_template("index.html", allowed=", ".join(sorted(ALLOWED_EXTENSIONS)), csrf=csrf)


@app.route("/upload", methods=["POST"])
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
def login():
    next_url = request.args.get("next") or url_for("protected")
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == PASSWORD:
            session["auth"] = True
            ensure_csrf()
            flash("登录成功", "success")
            return redirect(next_url)
        flash("密码错误", "error")
        return render_template("login.html", next=next_url)
    return render_template("login.html", next=next_url)


@app.route("/logout")
def logout():
    session.pop("auth", None)
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
    if ENABLE_SSL:
        # Development-only adhoc TLS for testing. In production terminate TLS at reverse proxy.
        app.run(debug=True, host="0.0.0.0", port=5000, ssl_context="adhoc")
    else:
        app.run(debug=True, host="0.0.0.0", port=5000)
