#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose --profile tls run --rm certbot renew --webroot --webroot-path=/var/www/certbot
docker compose --profile tls exec -T nginx nginx -t
docker compose --profile tls exec -T nginx nginx -s reload
