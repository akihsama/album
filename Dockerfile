FROM python:3.11-slim

# Pillow / argon2-cffi 都有官方 wheel，不需要 build-essential（能省掉上百 MB 镜像体积）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python deps
COPY requirements.txt /app/
RUN python -m pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . /app

# 原图 / 缩略图 / 数据库
RUN mkdir -p /app/uploads /app/thumbs /app/data

EXPOSE 8000

ENV PYTHONUNBUFFERED=1
ENV UPLOAD_FOLDER=/app/uploads
ENV THUMB_FOLDER=/app/thumbs
ENV DB_PATH=/app/data/album.db

# 2 worker × 4 线程：worker 太多会让缩略图内存峰值叠加
# （4000x3000 解码后 ~36MB，单张峰值几百 MB，小内存机器撑不住）
CMD ["gunicorn", "-w", "2", "--threads", "4", "-b", "0.0.0.0:8000", "--timeout", "120", "app:app"]
