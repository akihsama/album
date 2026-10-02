#!/usr/bin/env bash
# Generate a self-signed cert into ./certs/live/<domain>/ for local testing only.
# Usage: ./scripts/generate-self-signed-cert.sh example.com
set -e
DOMAIN=${1:-"example.com"}
OUTDIR="$(pwd)/certs/live/${DOMAIN}"
mkdir -p "${OUTDIR}"

echo "Generating self-signed cert for ${DOMAIN} -> ${OUTDIR}"

openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout "${OUTDIR}/privkey.pem" \
  -out "${OUTDIR}/fullchain.pem" \
  -subj "/C=US/ST=State/L=City/O=Example/OU=Dev/CN=${DOMAIN}"

echo "Done. You can now run: docker-compose up -d"
