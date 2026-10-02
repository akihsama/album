FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python deps
COPY requirements.txt /app/
RUN python -m pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . /app

# Create uploads directory and ensure permissions
RUN mkdir -p /app/uploads && chown -R root:root /app/uploads

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

# Run with Gunicorn, bind to 0.0.0.0:8000
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8000", "app:app"]
