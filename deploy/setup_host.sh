#!/usr/bin/env bash
# Einmalige Host-Einrichtung (Debian 13 LXC). Ausführen mit:  sudo bash deploy/setup_host.sh
# Löscht nichts. Ändert: APT-Quellen (+Docker-Repo), installiert Docker, aktiviert docker.service,
# fügt den aufrufenden User zur Gruppe docker hinzu, setzt Zeitzone Europe/Berlin.
set -euo pipefail

TARGET_USER="${SUDO_USER:-}"
if [[ $EUID -ne 0 || -z "$TARGET_USER" ]]; then
  echo "Bitte als normaler User mit sudo starten: sudo bash $0" >&2
  exit 1
fi

echo "==> Basis-Pakete"
apt-get update
apt-get install -y ca-certificates curl

echo "==> Offizielles Docker-Repository"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: ${CODENAME}
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF

echo "==> Docker Engine + Compose-Plugin"
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

echo "==> User ${TARGET_USER} -> Gruppe docker"
usermod -aG docker "$TARGET_USER"

echo "==> Zeitzone"
timedatectl set-timezone Europe/Berlin

echo "==> Test"
docker run --rm hello-world >/dev/null && echo "Docker läuft."
docker compose version
echo "Fertig. Bitte einmal ab- und wieder anmelden (oder 'newgrp docker'), damit die Gruppe greift."
