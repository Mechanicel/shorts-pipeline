#!/usr/bin/env bash
# Installiert/aktualisiert die systemd-Timer. Ausführen mit:  sudo bash deploy/install_timers.sh
# Ändert: /etc/systemd/system/shorts-*.{service,timer}, daemon-reload, aktiviert die Timer. Löscht nichts.
set -euo pipefail
TARGET_USER="${SUDO_USER:-}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
[[ $EUID -ne 0 || -z "$TARGET_USER" ]] && { echo "Bitte mit sudo als normaler User starten." >&2; exit 1; }

chmod +x "$DIR/deploy/notify_failure.sh"
for f in "$DIR"/deploy/systemd/*; do
  name="$(basename "$f")"
  sed -e "s|__USER__|${TARGET_USER}|g" -e "s|__DIR__|${DIR}|g" "$f" > "/etc/systemd/system/${name}"
  echo "installiert: /etc/systemd/system/${name}"
done
# Fehlermeldungen brauchen Lesezugriff aufs Journal
usermod -aG systemd-journal "$TARGET_USER"
systemctl daemon-reload
systemctl enable --now shorts-pipeline.timer shorts-upload.timer
systemctl list-timers 'shorts-*' --no-pager
