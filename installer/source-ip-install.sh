#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${MULTISUB_PANEL_DIR:-/opt/multisub-panel}"
STAGE_DIR="${SOURCE_IP_STAGE_DIR:-/home/phase/source-ip-release}"
SERVICE="${MULTISUB_PANEL_SERVICE:-multisub-panel}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/home/phase/multisub-panel-backup-source-ip-${STAMP}"

[[ "${EUID}" -eq 0 ]] || { echo "Run with sudo: sudo bash $0" >&2; exit 1; }
[[ -s "${STAGE_DIR}/source-ip-apply.py" && -s "${STAGE_DIR}/panel-cosmic.css" ]] || { echo "Staged source IP files are missing" >&2; exit 1; }
[[ -f "${APP_DIR}/app/panel.py" && -f "${APP_DIR}/templates/sources.html" ]] || { echo "Panel files are missing under ${APP_DIR}" >&2; exit 1; }

mkdir -p "${BACKUP}/app" "${BACKUP}/templates" "${BACKUP}/app/static"
cp -a "${APP_DIR}/app/panel.py" "${BACKUP}/app/panel.py"
cp -a "${APP_DIR}/templates/sources.html" "${BACKUP}/templates/sources.html"
if [[ -e "${APP_DIR}/app/static/panel-cosmic.css" ]]; then
  cp -a "${APP_DIR}/app/static/panel-cosmic.css" "${BACKUP}/app/static/panel-cosmic.css"
else
  touch "${BACKUP}/app/static/.panel-cosmic.css-was-absent"
fi

restore_on_error() {
  local code=$?
  trap - ERR
  cp -a "${BACKUP}/app/panel.py" "${APP_DIR}/app/panel.py" || true
  cp -a "${BACKUP}/templates/sources.html" "${APP_DIR}/templates/sources.html" || true
  if [[ -e "${BACKUP}/app/static/.panel-cosmic.css-was-absent" ]]; then
    rm -f "${APP_DIR}/app/static/panel-cosmic.css"
  else
    cp -a "${BACKUP}/app/static/panel-cosmic.css" "${APP_DIR}/app/static/panel-cosmic.css" || true
  fi
  systemctl restart "${SERVICE}" || true
  echo "Install failed; previous files restored from ${BACKUP}" >&2
  exit "${code}"
}
trap restore_on_error ERR

python3 "${STAGE_DIR}/source-ip-apply.py" "${APP_DIR}"
install -o root -g root -m 0644 "${STAGE_DIR}/panel-cosmic.css" "${APP_DIR}/app/static/panel-cosmic.css"
systemctl restart "${SERVICE}"
for attempt in {1..15}; do
  if curl -fsS --max-time 2 http://127.0.0.1:18082/login -o /dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 2 http://127.0.0.1:18082/login -o /dev/null
trap - ERR
echo "Source IP display installed. Backup: ${BACKUP}"
