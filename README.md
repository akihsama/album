Deployment: Docker + Nginx (TLS termination)

What I added
- Dockerfile: runs the Flask app with Gunicorn on port 8000.
- docker-compose.yml: defines two services: app and nginx. Nginx listens on 80/443 and proxies to the app service.
- nginx/ conf: example nginx config that redirects HTTP->HTTPS and proxies / to the Gunicorn app. TLS cert paths point to /etc/letsencrypt/live/<domain>/fullchain.pem.
- scripts/generate-self-signed-cert.sh: create a self-signed cert for local testing (drops into ./certs/live/<domain>/).
- .env.example: example environment file for docker-compose variables.

Quick start (testing with self-signed cert)
1. Copy the example env: cp .env.example .env
   - SECRET_KEY: openssl rand -hex 32
   - PASSWORD_HASH: python scripts/init_password.py （不要再写明文口令）
2. Generate a local self-signed cert (for testing):
   chmod +x scripts/generate-self-signed-cert.sh
   ./scripts/generate-self-signed-cert.sh example.com
   This writes cert files to ./certs/live/example.com/fullchain.pem and privkey.pem.
3. Update nginx/conf.d/site.conf: replace server_name example.com with the domain you used.
4. Start services:
   docker compose up -d --build
5. Visit:
   https://localhost (you may need to accept the self-signed cert in your browser) or https://example.com if DNS points to the host.

Production notes (Let's Encrypt)
- Obtain real certs for your domain and place them (or use a cert manager) under ./certs/live/<your-domain>/fullchain.pem and privkey.pem. Then docker-compose will make them available to nginx.
- Better: use an automated reverse-proxy + Let's Encrypt companion (nginx-proxy + docker-letsencrypt-nginx-proxy-companion) or use a host-managed certificate (cloud load balancer or Traefik/Caddy which integrate with ACME).
- Configure your DNS to point the domain to the host IP and run certbot to obtain certs, or use a managed solution.

Security reminders
- Do NOT use self-signed certs in production.
- Ensure SECRET_KEY and PASSWORD_HASH are strong and injected from a secure source (secrets manager / environment not committed to repo). Both are required: the app refuses to start if either is missing.
- Never run with `debug=True`. The Werkzeug debugger exposes an interactive console.
- Run tests before deploying: `python -m unittest discover tests`
- Use a hardened nginx config and enable HSTS only after confirming TLS works.
- Consider running the app under a non-root user inside the container and set proper file permissions for uploads.

If you want, I can:
- Add an automated nginx-proxy + Let's Encrypt companion docker-compose setup (zero-config for certs) OR
- Provide a Traefik-based docker-compose that automatically obtains/refreshes certificates from Let's Encrypt OR
- Create a cloud-specific deployment guide (e.g., Google Cloud Run, AWS ECS, DigitalOcean App Platform).

Tell me which option you prefer and I'll add it to the repo and push the changes.
