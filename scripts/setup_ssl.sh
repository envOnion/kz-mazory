#!/usr/bin/env bash
set -e

# ==============================================================================
# Mazory Let's Encrypt SSL Certificate Setup via Certbot & Docker Compose
# ==============================================================================

if [ -z "$1" ]; then
    echo "Использование: $0 <домен> [email]"
    echo "Пример: $0 crm.aquakip.kz admin@aquakip.kz"
    exit 1
fi

DOMAIN=$1
EMAIL=${2:-"admin@$DOMAIN"}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT_DIR"

echo "=========================================================="
echo "🔒 Настройка SSL-сертификата для домена: $DOMAIN"
echo "📧 Контактный email: $EMAIL"
echo "=========================================================="

# Создаем директории для Certbot
mkdir -p "$ROOT_DIR/certbot/conf"
mkdir -p "$ROOT_DIR/certbot/www"

# 1. Запуск Nginx в режиме HTTP (если еще не запущен)
echo "▶ 1. Проверка и запуск Nginx на порту 80..."
docker compose up -d nginx

# 2. Выпуск сертификата через контейнер certbot
echo "▶ 2. Запрос сертификата в Let's Encrypt..."
docker compose run --rm certbot certonly \
    --webroot \
    --webroot-path=/var/www/certbot \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    -d "$DOMAIN"

# 3. Применение SSL конфигурации Nginx
echo "▶ 3. Применение SSL конфигурации Nginx..."
sed "s/\${DOMAIN}/$DOMAIN/g" "$ROOT_DIR/nginx/ssl.conf.template" > "$ROOT_DIR/nginx/default.conf"

# 4. Перезагрузка Nginx для активации HTTPS
echo "▶ 4. Перезагрузка Nginx..."
docker compose exec nginx nginx -s reload || docker compose restart nginx

echo ""
echo "=========================================================="
echo "✅ SSL СЕРТИФИКАТ УСПЕШНО УСТАНОВЛЕН И АКТИВИРОВАН!"
echo "🌐 Сайт доступен по адресу: https://$DOMAIN"
echo "⚙️ Django Unfold Admin:    https://$DOMAIN/admin/"
echo "📱 WAHA Dashboard:         https://$DOMAIN/admin/waha-dashboard/"
echo "🔌 REST API:               https://$DOMAIN/api/"
echo "=========================================================="
