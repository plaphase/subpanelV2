#!/usr/bin/env bash
set -Eeuo pipefail

fail() { printf '[ERROR] %s\n' "$1" >&2; exit 1; }
[[ "$EUID" -eq 0 ]] || fail "Требуются права root."

PUBLIC_HOST="${PUBLIC_HOST:-}"
TLS_EMAIL="${TLS_EMAIL:-}"
[[ -n "$PUBLIC_HOST" && -n "$TLS_EMAIL" ]] || fail "Нужны домен и email для Certbot."
command -v certbot >/dev/null 2>&1 || fail "Не установлен certbot."

printf '[INFO] Выпуск HTTPS-сертификата для %s...\n' "$PUBLIC_HOST"
certbot --nginx --non-interactive --agree-tos --email "$TLS_EMAIL" \
    --domains "$PUBLIC_HOST" --redirect || fail "Certbot не выпустил сертификат. Проверь DNS домена и доступность порта 80."

nginx -t || fail "Nginx отклонил HTTPS-конфигурацию."
systemctl reload nginx || fail "Не удалось применить HTTPS-конфигурацию."
printf '[OK] HTTPS включён, HTTP перенаправляется на HTTPS.\n'
