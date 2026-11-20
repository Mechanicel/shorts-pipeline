# shorts-pipeline – „Was ändert sich für dich?“

Automatisierte YouTube-Shorts über neue Gesetze, Preise, Fristen und Geldthemen in Deutschland.
Offizielle Quellen → Claude-Skript (nur Fakten aus dem Quelltext) → KI-Stimme → 1080x1920-Video mit
Untertiteln → Freigabe per Telegram → YouTube-Upload mit geplanter Veröffentlichung.

## Ablauf

```
So (Timer)   pipeline:  Quellen (RSS/HTML) ─► SQLite ─► Claude-Skript ─► TTS ─► ffmpeg-Video
dauerhaft    bot:       neues Video ─► Telegram (Video, Skript, Quelle, Belege) ─► [Freigeben]/[Ablehnen]
täglich      upload:    freigegeben ─► YouTube (privat, publishAt auf Wochen-Slots verteilt)
```

Status je Thema: `neu → skript → video → wartet_auf_freigabe → freigegeben → hochgeladen`
(daneben `verworfen`, `abgelehnt`, `fehler`). Fehler landen per Telegram auf dem Handy.

## Setup

1. **Host** (einmalig, bereits erledigt): `sudo bash deploy/setup_host.sh` – Docker, Gruppe `docker`, Zeitzone.
2. **Image bauen:** `docker compose build`
3. **Test ohne Keys:** `docker compose run --rm job pipeline --dry-run`
   → `output/dryrun/de/00001/short.mp4` (Beispieldaten, espeak-Stimme, „TESTVIDEO“-Badge)
4. **Zugangsdaten:** `cp .env.example .env && chmod 600 .env`, dann selbst ausfüllen.
5. **Hintergründe (optional):** Hochkant-Videos oder Bilder nach `assets/backgrounds/` legen
   (ohne Dateien wird ein animierter Farbverlauf genutzt).

## YouTube-Zugang (einmalig)

1. [Google Cloud Console](https://console.cloud.google.com/): Projekt anlegen, **YouTube Data API v3** aktivieren.
2. **OAuth-Zustimmungsbildschirm:** Typ „Extern“, dann auf **„In Produktion“** stellen
   (im Status „Testing“ verfällt das Refresh-Token nach 7 Tagen; die Warnung „nicht verifiziert“ beim Login ist für
   die eigene Nutzung ok).
3. **Anmeldedaten → OAuth-Client-ID → „Desktop-App“** → JSON herunterladen, als `client_secret.json` neben
   `auth_youtube.py` auf dem Laptop speichern.
4. Auf dem Laptop (PowerShell, Python installiert):
   ```powershell
   py -m pip install google-auth-oauthlib
   py auth_youtube.py --channel de
   ```
   Im Browser anmelden und **den richtigen Kanal** (ggf. Brand-Account) wählen → `token_de.json`.
5. Token auf den Server kopieren und absichern:
   ```powershell
   scp .\token_de.json <user>@<server-ip>:~/shorts-pipeline/credentials/
   ```
   ```bash
   chmod 600 ~/shorts-pipeline/credentials/token_de.json
   ```
   Danach `token_de.json` und `client_secret.json` auf dem Laptop löschen bzw. sicher verwahren.

> **API-Audit:** Uploads aus ungeprüften API-Projekten sperrt YouTube auf „privat“, auch mit `publishAt`.
> Für automatisches Veröffentlichen das [Audit-Formular](https://support.google.com/youtube/contact/yt_api_form)
> einreichen; bis dahin im YouTube Studio manuell auf „Öffentlich“/„Geplant“ stellen.

## Telegram-Bot

1. Bei [@BotFather](https://t.me/BotFather) `/newbot` → Token in `.env` (`TELEGRAM_BOT_TOKEN`).
2. Eigene Chat-ID: Bot starten (`docker compose up -d bot`), dem Bot schreiben,
   `docker compose logs bot | grep chat_id` → in `.env` (`TELEGRAM_CHAT_ID`), dann `docker compose up -d bot` erneut.
3. Der Bot schickt neue Videos mit Skript, Quelle und Belegen; Buttons **Freigeben/Ablehnen**.
   Er reagiert nur auf diese Chat-ID. `/status` zeigt die Themen je Status.

## Zeitplan (systemd)

`sudo bash deploy/install_timers.sh` installiert:

| Timer | Zeit | Job |
|---|---|---|
| `shorts-pipeline.timer` | So 06:00 | `pipeline --channel de` |
| `shorts-upload.timer` | täglich 09:30 (nach Quota-Reset 09:00 MEZ) | `upload --channel de` |

Schlägt ein Job fehl (auch wenn Docker gar nicht startet), meldet `shorts-failure@.service` das per Telegram.
Kontrolle: `systemctl list-timers 'shorts-*'`, Logs: `journalctl -u shorts-pipeline -e`.
Der Bot läuft per `restart: unless-stopped` dauerhaft (`docker compose up -d bot`).

## Befehle

| Befehl | Zweck |
|---|---|
| `docker compose run --rm job pipeline [--channel de] [--dry-run]` | kompletter Wochenlauf |
| `docker compose run --rm job collect` | nur Quellen abrufen |
| `docker compose run --rm job status [--dry-run]` | Themen je Status |
| `docker compose run --rm job pipeline --limit 1` | Testlauf mit genau einem Video |
| `docker compose run --rm job upload [--dry-run]` | freigegebene Videos hochladen (`--dry-run`: nur Slot-Plan) |
| `docker compose run --rm job retry <id>` | Thema im Status `fehler` erneut einreihen |
| `docker compose up -d bot` / `docker compose logs -f bot` | Review-Bot starten / Logs |
| `docker compose run --rm --entrypoint python job -m pytest -q` | Tests |

## Konfiguration

- `.env` – Keys, Modell (`ANTHROPIC_MODEL`), Stimme (`OPENAI_TTS_VOICE`), TTS-Provider, Telegram, Quota-Limits.
- `config/channels.yaml` – pro Kanal: Quellen, Sprache, Stimme/Sprechanweisung, Hashtags, Veröffentlichungs-Slots,
  Limits. Neuer Kanal = neuer Eintrag + `credentials/token_<key>.json`.

## Projektstruktur

```
src/shorts/      Python-Paket (config, db, sources, script, tts, subtitles, render, pipeline, notify, CLI)
config/          channels.yaml
assets/          Hintergründe
tests/           pytest
deploy/          setup_host.sh, install_timers.sh, notify_failure.sh, systemd-Units
auth_youtube.py  einmalig auf dem Laptop: YouTube-OAuth-Token erzeugen
data/ output/ credentials/   Laufzeitdaten – nicht im Git
```
