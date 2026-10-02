# Self-hosted deployment guide (no credit card required)

This project can be deployed on your own Linux VPS or any server that can run Docker Compose.

## 1) Prepare the server

Install Docker + Docker Compose on your Ubuntu/Debian VPS:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
```

## 2) Clone / deploy the app

```bash
git clone https://github.com/akihsama/album.git
cd album
cp .env.example .env
```

Edit `.env` and set:

```bash
PASSWORD=your-strong-password
SECRET_KEY=generate-a-random-secret-string
MAX_CONTENT_LENGTH=16777216
```

Create upload directory:

```bash
mkdir -p uploads
chmod 700 uploads
```

Set the Caddy domain:

```bash
# Edit Caddyfile
# Replace example.com with your actual domain
```

## 3) Start the container stack

```bash
docker compose -f docker-compose.vps.yml up -d --build
```

## 4) DNS and TLS

Point your domain A record to the server IP address, then wait a few minutes.
Once DNS is live, Caddy will automatically obtain a valid certificate from Let's Encrypt.

## 5) Access the site

Visit:

```text
https://your-domain.com/
```

Then go to `/login` and use the password set in `.env`.

## 6) Useful commands

```bash
docker compose -f docker-compose.vps.yml logs -f

docker compose -f docker-compose.vps.yml down

docker compose -f docker-compose.vps.yml pull

docker compose -f docker-compose.vps.yml up -d --force-recreate
```

## 7) Production notes

- Keep `PASSWORD` and `SECRET_KEY` in a secure environment file.
- Use a real domain and DNS record so Caddy can issue TLS certificates.
- For long-term file storage, attach a persistent disk or use object storage.
- For real production workloads, add a database-backed user system instead of only a single password.
