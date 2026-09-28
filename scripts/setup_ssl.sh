#!/usr/bin/env bash
set -euo pipefail
ssl_domain=${1:?Usage: setup_ssl.sh domain email}
ssl_email=${2:?Provide the certificate contact email}
[[ "$ssl_domain" =~ ^[a-zA-Z0-9.-]+$ && "$ssl_domain" == *.* && "$ssl_domain" != -* ]] || exit 2
cd "$(dirname "$0")/.."
mkdir -p certbot/conf certbot/www
docker compose --profile tls up -d nginx
docker compose --profile tls run --rm certbot certonly --webroot --webroot-path=/var/www/certbot --email "$ssl_email" --agree-tos --no-eff-email -d "$ssl_domain"
python3 - "$ssl_domain" <<'PYTHON'
from pathlib import Path
import sys
Path('nginx/default.conf').write_text(Path('nginx/ssl.conf.template').read_text().replace('${DOMAIN}',sys.argv[1]))
PYTHON
docker compose --profile tls exec -T nginx nginx -t
docker compose --profile tls exec -T nginx nginx -s reload
curl --fail --silent --show-error "https://$ssl_domain/api/health/ready/"
