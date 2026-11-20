# shorts-pipeline – „Was ändert sich für dich?“

Automatisierte YouTube-Shorts (25–35 s, hochkant) über neue Gesetze, Preise, Fristen und Geldthemen in
Deutschland. Offizielle Quellen → Claude-Skript (nur Fakten aus dem Quelltext, mit Belegen) → KI-Stimme →
Video mit Faktenkarten und Untertiteln → Freigabe per Telegram → YouTube-Upload.

## Ablauf

```
So 06:00 (Timer)  pipeline   Quellen (RSS/HTML) → SQLite → Claude-Skript → TTS → Whisper → ffmpeg-Video
dauerhaft         bot        neues Video → Telegram (Video, Sprechtext, Quelle, Belege, Kosten)
                             → [✅ Freigeben] / [❌ Ablehnen]
täglich 09:30     upload     UPLOAD_MODE=api:    freigegebene Videos → YouTube (privat + geplanter Termin)
                             UPLOAD_MODE=manual: am Termin-Tag Upload-Paket an Telegram, du lädst selbst hoch
```

Status je Thema: `neu → skript → video → wartet_auf_freigabe → freigegeben → hochgeladen`
(daneben `verworfen`, `abgelehnt`, `fehler`; manueller Modus: `freigegeben → paket_gesendet → hochgeladen`).
Fehler und Zusammenfassungen kommen per Telegram aufs Handy.

## Webinterface

Im Heimnetz unter **`http://<server-ip>:8080`** (Dienst `web`, startet mit Docker automatisch).
Beim ersten Aufruf legst du ein Passwort fest (nur als scrypt-Hash gespeichert); danach Anmeldung,
Sperre nach 5 Fehlversuchen. Passwort vergessen: `docker compose run --rm job web-reset-password`.

- **Übersicht:** neue Videos erstellen (1–7), Videos ansehen mit Sprechtext, Belegen, Warnungen und Kosten,
  Freigeben / Ablehnen / Neu erzeugen, freigegebene Videos hochladen (nächster Termin, Wunschtermin oder sofort),
  Sichtbarkeit prüfen, zuletzt hochgeladene Videos mit YouTube-/Studio-Links, Job-Verlauf mit Logs, Kosten 7/30 Tage.
- **Zeitplan & Einstellungen:** an welchen Wochentagen und um wie viel Uhr Videos erstellt werden, wie viele pro
  Lauf, Upload-Uhrzeit, Veröffentlichungstermine, Passwort ändern.

Der Zeitplan läuft im Web-Dienst (APScheduler) und ist anfangs **aus**. Solange die systemd-Timer (siehe unten)
aktiv sind, nicht einschalten – sonst laufen Erstellen und Upload doppelt. Port 8080 nicht ins Internet freigeben.

## Ein Video von Hand erstellen und posten

Alle Befehle im Projektordner:

```bash
cd ~/shorts-pipeline

# 1. Video erzeugen (ca. 2 Min.), erscheint danach zur Freigabe in Telegram
docker compose run --rm job pipeline --channel de --limit 1

# 2. In Telegram prüfen und „Freigeben“ drücken

# 3. Hochladen – eine der drei Varianten:
docker compose run --rm job upload --channel de                                # nächster freier Termin
docker compose run --rm job upload --channel de --publish-at "2026-11-21 18:00"  # Wunschtermin (Berlin)
docker compose run --rm job upload --channel de --sofort                       # sofort öffentlich
```

`upload` lädt **alle** freigegebenen Videos hoch und gibt am Ende je Video die Video-ID aus.
`--publish-at` gilt für das erste Video, `--sofort` für alle Videos dieses Laufs.
Prüfen, ob die Videos für Zuschauer sichtbar sind (ohne Anmeldung): `docker compose run --rm job check-public`.

## Befehle

| Befehl | Zweck |
|---|---|
| `pipeline [--limit N] [--dry-run]` | Quellen abrufen, Skripte schreiben, Videos rendern (`--dry-run`: ohne Keys, Testdaten) |
| `upload [--publish-at …] [--sofort] [--mode api\|manual] [--dry-run]` | freigegebene Videos hochladen bzw. Upload-Pakete schicken |
| `check-public [--id ID]` | Sichtbarkeit hochgeladener Videos von außen prüfen |
| `status` | Anzahl Themen je Status |
| `collect` | nur Quellen abrufen |
| `regenerate <id>` | Thema mit neuem Skript + Video neu einreihen (alte Telegram-Buttons werden entfernt) |
| `retry <id>` | Thema im Status `fehler` erneut einreihen |
| `auth-check` | YouTube-Token prüfen (erzwungener Refresh, keine Quota) |
| `bot` | Telegram-Review-Bot (läuft als Dienst, siehe unten) |
| `web [--port 8080]` | Webinterface + Zeitplan (läuft als Dienst) |
| `web-reset-password` | Webinterface-Passwort zurücksetzen |

Jeweils als `docker compose run --rm job <befehl> [--channel de]`.
Tests: `docker compose run --rm --entrypoint python job -m pytest -q`.

## Setup (einmalig)

1. **Host:** `sudo bash deploy/setup_host.sh` – Docker + Compose aus dem offiziellen Repo, Gruppe `docker`,
   Zeitzone Europe/Berlin. Danach neu anmelden.
2. **Image:** `docker compose build`
3. **Test ohne Keys:** `docker compose run --rm job pipeline --dry-run` → `output/dryrun/de/00001/short.mp4`
   (espeak-Teststimme, klingt absichtlich roboterhaft).
4. **Zugangsdaten:** `cp .env.example .env && chmod 600 .env`, dann selbst ausfüllen.
5. **Telegram-Bot:** bei [@BotFather](https://t.me/BotFather) `/newbot` → Token in `.env`.
   Chat-ID: `docker compose up -d bot`, dem Bot schreiben, `docker compose logs bot | grep chat_id`,
   in `.env` eintragen, `docker compose up -d bot` erneut. Der Bot reagiert nur auf diese Chat-ID.
6. **YouTube:** siehe unten.
7. **Zeitplan:** `sudo bash deploy/install_timers.sh`

### YouTube-Zugang

1. [Google Cloud Console](https://console.cloud.google.com/): Projekt anlegen, **YouTube Data API v3** aktivieren.
2. OAuth-Zustimmungsbildschirm „Extern“, auf **„In Produktion“** stellen (im Status „Testing“ verfällt das
   Refresh-Token nach 7 Tagen).
3. OAuth-Client vom Typ **„Desktop-App“** anlegen, JSON als `client_secret.json` herunterladen.
4. Auf dem Laptop (PowerShell, im Ordner mit der JSON):
   ```powershell
   scp <ssh-alias>:~/shorts-pipeline/auth_youtube.py .
   py -m pip install --upgrade google-auth-oauthlib
   py auth_youtube.py --channel de --client-secret (Get-ChildItem .\client_secret*.json)[0].FullName
   scp .\token_de.json <ssh-alias>:~/shorts-pipeline/credentials/token_de.json
   ssh <ssh-alias> "chmod 600 ~/shorts-pipeline/credentials/token_de.json"
   ```
5. Prüfen: `docker compose run --rm job auth-check`

> **API-Audit:** Laut Google-Hilfe (support.google.com/youtube/answer/7300965) werden Uploads aus ungeprüften
> API-Projekten als privat gesperrt. In der Praxis (Nov. 2026) funktionierten trotzdem: von Hand in Studio
> veröffentlichen, geplante Veröffentlichung (`publishAt`) und `--sofort` – jeweils von außen geprüft
> (`check-public`). Das kann sich ändern; für Dauerbetrieb das
> [Audit](https://support.google.com/youtube/contact/yt_api_form) beantragen.
> Fallback: `UPLOAD_MODE=manual` in `.env`.

## Zeitplan (systemd – wird durch den Zeitplan im Webinterface ersetzt)

| Timer | Zeit | Job |
|---|---|---|
| `shorts-pipeline.timer` | So 06:00 (+ bis 15 Min.) | `pipeline --channel de` – bis zu 7 Videos |
| `shorts-upload.timer` | täglich 09:30 (+ bis 10 Min., nach dem Quota-Reset 09:00) | `upload --channel de` |

Termine (`config/channels.yaml`): Mo–Fr 17:00, Sa/So 11:00, mind. 2 h Vorlauf, max. 5 Uploads/Tag
(1 Upload ≈ 1.600 von 10.000 Einheiten). Schlägt ein Job fehl, meldet `shorts-failure@.service` das per Telegram.
Kontrolle: `systemctl list-timers 'shorts-*'`, Logs: `journalctl -u shorts-pipeline -e`.
Der Bot läuft per `restart: unless-stopped` dauerhaft (`docker compose up -d bot`, Logs: `docker compose logs -f bot`).

## Videos: Inhalt und Gestaltung

- **Themenauswahl:** Vorfilter (Titel-Begriffe in `channels.yaml`, kostenlos) → Vorauswahl mit Claude Haiku
  (nur Titel + Anriss, Bruchteile eines Cents) → Datumsprüfung → erst dann das Skript mit Opus.
- **Skript:** 60–75 Wörter. Erster Satz max. 8 Wörter, du-Ansprache, konkreter Nutzen/Verlust, keine
  Datums-Einleitung, Rechtsstatus exakt (geplant ≠ beschlossen). Jede Zahl braucht ein wörtliches Zitat aus
  der Quelle; der Code prüft Zitate und Grafik-Zahlen automatisch und warnt in Telegram.
- **Bild:** Faktenkarten (Navy `#0B1F3A`, Gelb `#FFD23F`, Montserrat) mit Kernzahl ab Frame 0, kleine Titelbox
  ab 1,2 s, Untertitel mit hervorgehobenem Wort, keine Endkarte (sauberer Loop; Quelle steht in der Beschreibung).
- **Hintergrund:** eigene Clips in `assets/backgrounds/` → Pixabay (mit `PIXABAY_API_KEY`; 24-h-Suchcache,
  Download statt Hotlink, max. 3 neue Clips/Lauf, Credit automatisch in der Beschreibung) → selbst generierte
  Animation. Kein fremdes Gameplay (Urheberrecht).
- **Stimme:** OpenAI-TTS (WAV), um `TTS_SPEED` (Standard 1,25) beschleunigt, Untertitel-Timing per Whisper.
- **Kosten:** je Video in der Telegram-Review (Claude exakt aus Tokens, TTS/Whisper geschätzt), Summe pro Lauf
  in der Zusammenfassung. Richtwert: ca. 0,08–0,10 € pro Video.

## Konfiguration

- `.env` – Keys, Modell (`ANTHROPIC_MODEL`), Stimme/Tempo (`OPENAI_TTS_VOICE`, `TTS_SPEED`), `UPLOAD_MODE`,
  Pixabay, Telegram, Quota-Limits, Kostensätze. Vorlage: `.env.example`.
- `config/channels.yaml` – pro Kanal: Quellen, Sprache, Sprechanweisung, Hashtags, Termine, Limits,
  Pixabay-Suchbegriffe. Neuer Kanal = neuer Eintrag + `credentials/token_<key>.json`.

## Projektstruktur

```
src/shorts/
  __main__.py     CLI
  config.py       .env + channels.yaml
  db.py           SQLite + Status-Maschine, Kosten, Quota
  sources.py      RSS/HTML-Quellen, Artikeltext
  script.py       Claude-Skript, Hook-/Längen-/Belegprüfung
  tts.py          TTS-Engines (OpenAI, espeak) + Tempo
  subtitles.py    Wort-Timings, ASS-Untertitel
  graphics.py     Faktenkarten
  backgrounds.py  eigene Clips / Pixabay / generierte Animation
  render.py       ffmpeg-Rendering
  pipeline.py     Orchestrierung des Wochenlaufs
  review_bot.py   Telegram-Freigabe
  upload.py       YouTube-Upload (API)
  manual.py       Upload-Pakete (manueller Modus)
  actions.py      Freigeben/Ablehnen/Neu erzeugen (gemeinsam für Bot, CLI, Web)
  web/            Webinterface: app.py (Routen), auth.py, schedule.py (Zeitplan), jobs.py, templates/
  publiccheck.py  Sichtbarkeitsprüfung von außen
  costs.py        Kostenberechnung
  notify.py       Telegram-Meldungen
config/           channels.yaml
deploy/           setup_host.sh, install_timers.sh, notify_failure.sh, systemd-Units
tests/            pytest
auth_youtube.py   einmalig auf dem Laptop: YouTube-OAuth-Token erzeugen
assets/ data/ output/ credentials/   Hintergründe bzw. Laufzeitdaten – data/, output/, credentials/ nicht im Git
```
