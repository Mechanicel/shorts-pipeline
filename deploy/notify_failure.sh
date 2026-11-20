#!/usr/bin/env bash
# Meldet einen fehlgeschlagenen systemd-Job per Telegram – auch wenn Docker/Python gar nicht starten.
set -euo pipefail
cd "$(dirname "$0")/.."
unit="${1:-unbekannt}"
get() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'\r'; }
token="$(get TELEGRAM_BOT_TOKEN)"; chat="$(get TELEGRAM_CHAT_ID)"
[[ -z "$token" || -z "$chat" ]] && { echo "Telegram nicht konfiguriert"; exit 0; }
log="$(journalctl -u "$unit" -n 15 --no-pager -o cat 2>/dev/null | tail -c 3000 || true)"
curl -fsS --max-time 20 "https://api.telegram.org/bot${token}/sendMessage" \
  --data-urlencode "chat_id=${chat}" \
  --data-urlencode "text=❌ systemd-Job ${unit} fehlgeschlagen auf $(hostname):
${log}" >/dev/null
