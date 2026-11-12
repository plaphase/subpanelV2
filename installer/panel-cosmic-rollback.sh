#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${MULTISUB_PANEL_DIR:-/opt/multisub-panel}"
SERVICE="${MULTISUB_PANEL_SERVICE:-multisub-panel}"
BACKUP="${1:-}"
if [[ -z "${BACKUP}" ]]; then
  BACKUP="$(find /home/phase -maxdepth 1 -type d -name 'multisub-panel-backup-panel-cosmic-*' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
fi
[[ "${EUID}" -eq 0 ]] || { echo "Run with sudo: sudo bash $0 [backup-directory]" >&2; exit 1; }
[[ -n "${BACKUP}" && -d "${BACKUP}/templates" ]] || { echo "Theme backup not found" >&2; exit 1; }
for file in index.html user.html new_user.html dashboard.html sources.html logs.html bulk.html bulk-result.html bulk-add-result.html trash.html; do
  [[ -f "${BACKUP}/templates/${file}" ]] || { echo "Incomplete backup: ${file}" >&2; exit 1; }
  cp -a "${BACKUP}/templates/${file}" "${APP_DIR}/templates/${file}"
done
if [[ -e "${BACKUP}/app/static/.panel-cosmic.css-was-absent" ]]; then
  rm -f "${APP_DIR}/app/static/panel-cosmic.css"
elif [[ -f "${BACKUP}/app/static/panel-cosmic.css" ]]; then
  cp -a "${BACKUP}/app/static/panel-cosmic.css" "${APP_DIR}/app/static/panel-cosmic.css"
fi
systemctl restart "${SERVICE}"
echo "Panel theme rolled back from ${BACKUP}"
