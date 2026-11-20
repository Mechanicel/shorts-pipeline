# CLAUDE.md – shorts-pipeline

## Ziel
Automatisierte YouTube-Shorts für den Kanal **„Was ändert sich für dich?“**: 30–45 s Hochkant-Videos über
neue Gesetze, Preise, Fristen und Geldthemen in Deutschland. Kein Gesicht, KI-Stimme, große Texteinblendungen.
Aufwand des Betreibers: max. 2 h/Woche, nur Faktencheck + Freigabe per Telegram.
Zunächst ein deutscher Kanal; Architektur ist pro Kanal/Sprache konfigurierbar (später englischer Kanal mit
eigenen Quellen → neuer Eintrag in `config/channels.yaml`, eigene `credentials/token_<key>.json`).

## Umgebung
- Debian-13-Container, Details (Netz, Hostnamen) in `CLAUDE.local.md` (nicht im Git).
- Docker Engine + Compose-Plugin aus dem offiziellen Repo; Host-User ist in der Gruppe `docker`. Zeitzone Europe/Berlin.
- `sudo` braucht ein Passwort → Claude kann sudo nicht selbst ausführen; Befehle als Skript vorbereiten.
- Alles läuft in Docker (`python:3.12-slim` + ffmpeg/libass + Montserrat + espeak-ng). `src/`, `tests/`, `config/`,
  `assets/` sind in die Container gemountet → Code-Änderungen ohne Rebuild; nur `requirements.txt`/`Dockerfile` brauchen `docker compose build`.

## Regeln (vom Betreiber)
- **Vor jedem sudo-Befehl, der etwas löscht oder Systemdienste ändert: fragen.**
- **SSH-Konfiguration nicht anfassen** (Key-only ist eingerichtet).
- **Nie nach API-Keys im Chat fragen.** Nur `.env.example` pflegen; `.env` füllt der Betreiber selbst.
- `.env`, `credentials/`, `data/`, `output/`, `CLAUDE.local.md` niemals committen (`.gitignore`).
- **Repo ist öffentlich:** vor jedem Push getrackte Dateien auf IPs, Mailadressen, Keys/Tokens prüfen
  (`git grep --cached -nE '...'`), interne Infrastruktur nur in `CLAUDE.local.md`.
- Erklärungen knapp; Betreiber ist Entwickler (Python/Java). Schrittweise vorgehen, nach größeren Schritten kurz berichten.
- Inhaltlich: Skripte nur aus Fakten des Quelltexts; bei Unsicherheit Thema verwerfen.

## Architektur (`src/shorts/`)
| Modul | Aufgabe |
|---|---|
| `config.py` | `.env` → `Settings`, `config/channels.yaml` → `ChannelConfig` (Quellen, Stimme, Slots, Limits) |
| `db.py` | SQLite (`data/pipeline.sqlite3`, WAL) + Status-Maschine mit erlaubten Übergängen, Quota-Tabelle |
| `sources.py` | RSS (feedparser) + HTML-Linklisten (Regex), URL-Normalisierung, Artikeltext via trafilatura |
| `script.py` | Claude (`messages.parse` + Pydantic `ShortScript`, Modell/Effort aus `.env`, Refusal-Fallback). Validierung 70–110 Wörter, 1 Korrekturversuch. Belege (`evidence`) mit wörtlichen Zitaten → `unverified_quotes()` prüft gegen Quelltext |
| `tts.py` | `TTSEngine`-Protocol; `OpenAITTS`, `EspeakTTS` (Dry-Run). Piper/ElevenLabs: neue Klasse + `get_tts()` |
| `subtitles.py` | Wort-Timings (Schätzung oder Whisper-Wortzeitstempel), Chunking, ASS-Datei (Titel, Untertitel mit gelbem aktivem Wort, Endkarte mit Quelle) |
| `render.py` | ffmpeg: 1080x1920, Hintergrund aus `assets/backgrounds/` (Video/Bild, sonst Farbverlauf), ASS eingebrannt, loudnorm −14 LUFS |
| `pipeline.py` | Orchestrierung collect → script → render, Fehler pro Thema → Status `fehler`, Zusammenfassung per Telegram |
| `notify.py` | Telegram-`sendMessage` für Fehler/Status (no-op ohne Konfiguration) |
| `review_bot.py` | *(Schritt 3)* Telegram-Bot: Video+Skript+Quelle+Belege, Buttons Freigeben/Ablehnen, nur `TELEGRAM_CHAT_ID` |
| `upload.py` | *(Schritt 3)* YouTube Data API v3, privat + `publishAt` auf Slots verteilt, Quota-Tracking |
| `__main__.py` | CLI: `pipeline [--dry-run]`, `collect`, `status`, `retry <id>`, `bot`, `upload [--dry-run]` |

Status: `neu → skript → video → wartet_auf_freigabe → freigegeben → hochgeladen`; Nebenwege `verworfen`
(Claude/zu kurz/veraltet >21 Tage), `abgelehnt` (Betreiber), `fehler` (retrybar via `retry`).

Dry-Run: eigene DB `data/dryrun.sqlite3` + `output/dryrun/`, wird pro Lauf zurückgesetzt; Beispieldaten in
`sample_data.py` (fiktiv, Video mit „TESTVIDEO“-Badge); espeak statt OpenAI, kein Claude-Aufruf.

## Befehle
```bash
docker compose build
docker compose run --rm job pipeline --dry-run      # Testvideo → output/dryrun/de/00001/short.mp4
docker compose run --rm job pipeline --channel de   # echter Lauf
docker compose run --rm --entrypoint python job -m pytest -q
docker compose up -d bot
```

## Wichtige Fakten / Stolpersteine
- YouTube: ungeprüfte API-Projekte → Uploads dauerhaft privat; API-Audit beantragen. OAuth-Consent auf „In Produktion“,
  sonst läuft das Refresh-Token nach 7 Tagen ab. Upload ≈ 1.600 Einheiten, Tageskontingent 10.000 (Reset Mitternacht Pacific Time).
- Verbraucherzentrale hat kein RSS → `type: html` mit `link_pattern` (Startseite, eher Ratgeber als News).
- Bundesregierung-Feeds: `/service/rss/breg-de/<id>/feed.xml` (1151246 Artikel, 1151244 Pressemitteilungen).
- feedparser liefert UTC-`struct_time` → `calendar.timegm`, nicht `mktime` (Container-TZ ist Berlin).

## Stand (2026-11-20)
- [x] Schritt 1: Infrastruktur (Docker, Gruppe, TZ, Projekt, git init, Dockerfile, Compose)
- [x] Schritt 2: Dry-Run-Pipeline bis Testvideo, 16 Tests grün, echte Quellen abrufbar (103 Themen in `data/pipeline.sqlite3`)
- [ ] Schritt 3: Telegram-Review-Bot, YouTube-Upload, `auth_youtube.py`, systemd-Timer (Pipeline So, Upload täglich)
- [ ] Erster echter Lauf mit API-Keys (Prompt-Qualität prüfen, Kosten beobachten)
- Offen/Ideen: eigene Hintergrundvideos in `assets/backgrounds/`, Piper als günstige TTS-Alternative, englischer Kanal.
