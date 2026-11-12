#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${MULTISUB_PANEL_DIR:-/opt/multisub-panel}"
STAGE_DIR="${SOURCE_ORDER_FIX_STAGE_DIR:-/home/phase/source-order-csrf-fix}"
SERVICE="${MULTISUB_PANEL_SERVICE:-multisub-panel}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/home/phase/multisub-panel-backup-source-order-${STAMP}"
TARGET="${APP_DIR}/templates/user.html"

[[ "${EUID}" -eq 0 ]] || { echo "Run with sudo: sudo bash $0" >&2; exit 1; }
[[ -f "${TARGET}" && -s "${STAGE_DIR}/source-order-csrf-fix.py" ]] || { echo "Panel template or patch file is missing" >&2; exit 1; }
mkdir -p "${BACKUP}/templates"
cp -a "${TARGET}" "${BACKUP}/templates/user.html"

restore_on_error() {
  local code=$?
  trap - ERR
  cp -a "${BACKUP}/templates/user.html" "${TARGET}" || true
  systemctl restart "${SERVICE}" || true
  echo "Patch failed; previous template restored from ${BACKUP}" >&2
  exit "${code}"
}
trap restore_on_error ERR

python3 "${STAGE_DIR}/source-order-csrf-fix.py" "${TARGET}"
systemctl restart "${SERVICE}"
for attempt in {1..15}; do
  if curl -fsS --max-time 2 http://127.0.0.1:18082/login -o /dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 2 http://127.0.0.1:18082/login -o /dev/null
trap - ERR
echo "Source order CSRF fix installed. Backup: ${BACKUP}"
