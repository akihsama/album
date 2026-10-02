import os
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

UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
ALLOWED_EXTENSIONS = {"txt", "pdf", "png", "jpg", "jpeg", "gif", "zip", "csv"}
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024))
PASSWORD = os.environ.get("PASSWORD", "changeme")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = SECRET_KEY

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return decorated


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", allowed=", ".join(sorted(ALLOWED_EXTENSIONS)))


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        flash("未检测到文件字段", "error")
        return redirect(url_for("index"))

    file = request.files["file"]
    if file.filename == "":
        flash("未选择文件", "error")
        return redirect(url_for("index"))

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(save_path)
        flash(f"文件已上传: {filename}", "success")
        return redirect(url_for("index"))

    flash("不支持的文件类型", "error")
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.args.get("next") or url_for("protected")
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == PASSWORD:
            session["auth"] = True
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
    files = sorted(os.listdir(app.config["UPLOAD_FOLDER"]))
    return render_template("protected.html", files=files)


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    safe_name = secure_filename(filename)
    if safe_name != filename:
        abort(404)
    return send_from_directory(app.config["UPLOAD_FOLDER"], safe_name, as_attachment=True)


@app.errorhandler(413)
def request_entity_too_large(error):
    flash(f"上传文件超出限制（最大 {app.config['MAX_CONTENT_LENGTH']} 字节）", "error")
    return redirect(url_for("index")), 413


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
