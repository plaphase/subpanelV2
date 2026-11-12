#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${MULTISUB_PANEL_DIR:-/opt/multisub-panel}"
STAGE_DIR="${PANEL_COSMIC_STAGE_DIR:-/home/phase/panel-cosmic-release}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/home/phase/multisub-panel-backup-panel-cosmic-${STAMP}"
SERVICE="${MULTISUB_PANEL_SERVICE:-multisub-panel}"
TEMPLATES=(index.html user.html new_user.html dashboard.html sources.html logs.html bulk.html bulk-result.html bulk-add-result.html trash.html)

[[ "${EUID}" -eq 0 ]] || { echo "Run with sudo: sudo bash $0" >&2; exit 1; }
[[ -d "${APP_DIR}/templates" && -d "${APP_DIR}/app/static" ]] || { echo "Panel directory not found: ${APP_DIR}" >&2; exit 1; }
[[ -s "${STAGE_DIR}/panel-cosmic.css" && -s "${STAGE_DIR}/panel-cosmic-apply.py" ]] || { echo "Staged theme files not found in ${STAGE_DIR}" >&2; exit 1; }

mkdir -p "${BACKUP}/templates" "${BACKUP}/app/static"
for file in "${TEMPLATES[@]}"; do cp -a "${APP_DIR}/templates/${file}" "${BACKUP}/templates/${file}"; done
if [[ -e "${APP_DIR}/app/static/panel-cosmic.css" ]]; then
  cp -a "${APP_DIR}/app/static/panel-cosmic.css" "${BACKUP}/app/static/panel-cosmic.css"
else
  touch "${BACKUP}/app/static/.panel-cosmic.css-was-absent"
fi
restore_backup() {
  for file in "${TEMPLATES[@]}"; do cp -a "${BACKUP}/templates/${file}" "${APP_DIR}/templates/${file}"; done
  if [[ -e "${BACKUP}/app/static/.panel-cosmic.css-was-absent" ]]; then
    rm -f "${APP_DIR}/app/static/panel-cosmic.css"
  else
    cp -a "${BACKUP}/app/static/panel-cosmic.css" "${APP_DIR}/app/static/panel-cosmic.css"
  fi
}
on_error() {
  local code=$?
  trap - ERR
  echo "Install failed; restoring ${BACKUP}" >&2
  restore_backup || true
  systemctl restart "${SERVICE}" || true
  exit "${code}"
}
trap on_error ERR

install -o root -g root -m 0644 "${STAGE_DIR}/panel-cosmic.css" "${APP_DIR}/app/static/panel-cosmic.css"
python3 "${STAGE_DIR}/panel-cosmic-apply.py" "${APP_DIR}"
systemctl restart "${SERVICE}"
for attempt in {1..15}; do
  if curl -fsS --max-time 2 http://127.0.0.1:18082/login -o /dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 2 http://127.0.0.1:18082/login -o /dev/null
trap - ERR
echo "Cosmic panel theme installed. Backup: ${BACKUP}"
