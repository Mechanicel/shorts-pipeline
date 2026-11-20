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

## Befehle

| Befehl | Zweck |
|---|---|
| `docker compose run --rm job pipeline [--channel de] [--dry-run]` | kompletter Wochenlauf |
| `docker compose run --rm job collect` | nur Quellen abrufen |
| `docker compose run --rm job status [--dry-run]` | Themen je Status |
| `docker compose run --rm job retry <id>` | Thema im Status `fehler` erneut einreihen |
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
deploy/          setup_host.sh, systemd-Units
data/ output/ credentials/   Laufzeitdaten – nicht im Git
```
