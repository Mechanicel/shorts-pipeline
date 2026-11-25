#!/usr/bin/env bash
# UI-Prüfung: Webinterface auf einer DB-Kopie starten, Playwright-Container macht Screenshots nach docs/ui/
# und prüft Höhe von „Heute“, horizontale Scrollleisten bei 390 px und Konsolenfehler.
set -euo pipefail
cd "$(dirname "$0")/.."
PW_IMAGE="mcr.microsoft.com/playwright/python:v1.49.1-noble"
APP="shorts-ui-check"
docker rm -f "$APP" >/dev/null 2>&1 || true
docker compose run -d --name "$APP" --entrypoint python job tests/ui/ui_app.py >/dev/null
trap 'docker rm -f "$APP" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do
  docker exec "$APP" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz')" \
    >/dev/null 2>&1 && break
  sleep 1
done
mkdir -p docs/ui
docker run --rm --network "container:$APP" -v "$PWD:/work" -w /work --user "$(id -u):$(id -g)" \
  -e HOME=/tmp "$PW_IMAGE" sh -c "pip install -q --user --break-system-packages playwright==1.49.1 2>/dev/null || pip install -q --user playwright==1.49.1; python tests/ui/ui_check.py"
