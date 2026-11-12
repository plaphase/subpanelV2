#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${MULTISUB_PANEL_DIR:-/opt/multisub-panel}"
SERVICE="${MULTISUB_PANEL_SERVICE:-multisub-panel}"
BACKUP="${1:-}"
if [[ -z "${BACKUP}" ]]; then
  BACKUP="$(find /home/phase -maxdepth 1 -type d -name 'multisub-panel-backup-source-ip-*' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
fi
[[ "${EUID}" -eq 0 ]] || { echo "Run with sudo: sudo bash $0 [backup-directory]" >&2; exit 1; }
[[ -n "${BACKUP}" && -f "${BACKUP}/app/panel.py" && -f "${BACKUP}/templates/sources.html" ]] || { echo "Source IP backup not found or incomplete" >&2; exit 1; }
cp -a "${BACKUP}/app/panel.py" "${APP_DIR}/app/panel.py"
cp -a "${BACKUP}/templates/sources.html" "${APP_DIR}/templates/sources.html"
if [[ -e "${BACKUP}/app/static/.panel-cosmic.css-was-absent" ]]; then
  rm -f "${APP_DIR}/app/static/panel-cosmic.css"
else
  cp -a "${BACKUP}/app/static/panel-cosmic.css" "${APP_DIR}/app/static/panel-cosmic.css"
fi
systemctl restart "${SERVICE}"
echo "Source IP changes rolled back from ${BACKUP}"
