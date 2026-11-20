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
| `script.py` | Claude (`messages.parse` + Pydantic `ShortScript`, Modell/Effort aus `.env`; kein Refusal-Fallback – Ablehnung = Thema verworfen). Validierung 70–110 Wörter, 1 Korrekturversuch. Belege (`evidence`) mit wörtlichen Zitaten → `unverified_quotes()` prüft gegen Quelltext |
| `tts.py` | `TTSEngine`-Protocol; `OpenAITTS`, `EspeakTTS` (Dry-Run). Piper/ElevenLabs: neue Klasse + `get_tts()` |
| `subtitles.py` | Wort-Timings (Standard: Whisper-Wortzeitstempel; `SUBTITLE_TIMING=estimate` als kostenlose Schätzung), Chunking, ASS-Datei (Titel, Untertitel mit gelbem aktivem Wort, Endkarte mit Quelle) |
| `backgrounds.py` | Hintergrund: eigene Clips (`assets/backgrounds/`) → Pixabay-Pool (`data/backgrounds/pixabay/`) → generierte Ball-Physik-Animation (`data/backgrounds/generated/`, 6 Varianten, gecacht) → Farbverlauf. Liefert `BackgroundChoice(path, credit)` |
| `graphics.py` | Faktenkarten-PNGs im Kanal-Design (Navy `#0B1F3A`, Gelb `#FFD23F`, Montserrat): Datums-Pill + große Zahl + Label, gleichmäßig über die Sprechzeit verteilt |
| `render.py` | ffmpeg: 1080x1920, Hintergrund auf 9:16 zugeschnitten + abgedunkelt + weichgezeichnet, Faktenkarten-Overlay, ASS eingebrannt, loudnorm −14 LUFS |
| `pipeline.py` | Orchestrierung collect → script → render, Fehler pro Thema → Status `fehler`, Zusammenfassung per Telegram |
| `notify.py` | Telegram-`sendMessage` für Fehler/Status (no-op ohne Konfiguration) |
| `review_bot.py` | Telegram-Bot (python-telegram-bot, JobQueue pollt alle 60 s Status `video`): Video+Skript+Quelle+Belege, Buttons Freigeben/Ablehnen, nur `TELEGRAM_CHAT_ID` |
| `upload.py` | YouTube Data API v3, privat + `publishAt` auf `publish_slots` (Berlin) verteilt, Quota je Pacific-Tag in SQLite, Token-Refresh schreibt `credentials/token_<key>.json` zurück |
| `__main__.py` | CLI: `pipeline [--dry-run] [--limit N]`, `collect`, `status`, `retry <id>`, `bot`, `upload [--dry-run]` |

Status: `neu → skript → video → wartet_auf_freigabe → freigegeben → hochgeladen`; Nebenwege `verworfen`
(Claude/zu kurz/veraltet >21 Tage), `abgelehnt` (Betreiber), `fehler` (retrybar via `retry`).

Dry-Run: eigene DB `data/dryrun.sqlite3` + `output/dryrun/`, wird pro Lauf zurückgesetzt; Beispieldaten in
`sample_data.py` (fiktiv, Video mit „TESTVIDEO“-Badge); espeak statt OpenAI, kein Claude-Aufruf.

Außerdem: `auth_youtube.py` (Repo-Root, läuft auf dem Laptop des Betreibers), `deploy/systemd/*` mit Platzhaltern
`__USER__`/`__DIR__` (eingesetzt von `deploy/install_timers.sh`), `deploy/notify_failure.sh` (OnFailure → Telegram via curl).

## Befehle
```bash
docker compose build
docker compose run --rm job pipeline --dry-run      # Testvideo → output/dryrun/de/00001/short.mp4
docker compose run --rm job pipeline --channel de   # echter Lauf
docker compose run --rm --entrypoint python job -m pytest -q
docker compose up -d bot
```

## Design- und Inhaltsregeln
- Kernzahlen + Datum stehen als **eigene Grafik groß im Vordergrund**; Stock-Material ist nur Hintergrund.
- Claude liefert `key_facts` (1–3, Ziffern wie in der Quelle) + `date_label`; `unverified_numbers()` prüft jede
  Grafik-Zahl gegen den Quelltext → Warnung im Telegram-Review.
- Stimme: `TTS_SPEED` (Standard 1.15, ffmpeg atempo vor Whisper), Skript 70–95 Wörter → ca. 32–43 s.
- **Kein fremdes Gameplay** (urheberrechtlich geschützt). Erlaubt: eigene Aufnahmen, Pixabay, eigene Animation.

## Pixabay-Regeln (verbindlich)
- Suchanfragen 24 h cachen (`search_*.json`), Videos herunterladen statt hotlinken, max. 100 Requests/Minute
  (Drossel 0,7 s), keine Massen-Downloads (max. 3 neue Clips pro Lauf, Pool max. 15).
- Pixabay hat kein `orientation` → Querformat wird beim Rendern auf 9:16 zugeschnitten (Variante ≥ 1080 px Höhe).
- Quellenangabe in der YouTube-Beschreibung: `Video: Pixabay <pageURL>` (Spalte `background_credit`).
- API-Key steht in der Request-URL → Exception-Messages von httpx nie loggen (nur den Typ).
- Pixabay sucht sehr locker ('kinetic sand' → Strände) → `matches_query()`: jedes Suchwort muss in den Tags stehen.
  Suchbegriffe in `channels.yaml` sind getestet; pro Lauf je Begriff max. 1 neuer Clip, Startbegriff rotiert.
- Pexels wurde verworfen (keine neuen Keys).

## Wichtige Fakten / Stolpersteine
- YouTube: ungeprüfte API-Projekte → Uploads dauerhaft privat; API-Audit beantragen. OAuth-Consent auf „In Produktion“,
  sonst läuft das Refresh-Token nach 7 Tagen ab. Upload ≈ 1.600 Einheiten, Tageskontingent 10.000 (Reset Mitternacht Pacific Time).
- Verbraucherzentrale hat kein RSS → `type: html` mit `link_pattern` (Startseite, eher Ratgeber als News).
- Bundesregierung-Feeds: `/service/rss/breg-de/<id>/feed.xml` (1151246 Artikel, 1151244 Pressemitteilungen).
- feedparser liefert UTC-`struct_time` → `calendar.timegm`, nicht `mktime` (Container-TZ ist Berlin).

## Stand (2026-11-20)
- [x] Schritt 1: Infrastruktur (Docker, Gruppe, TZ, Projekt, git init, Dockerfile, Compose)
- [x] Schritt 2: Dry-Run-Pipeline bis Testvideo; echte Quellen abrufbar (103 Themen in `data/pipeline.sqlite3`)
- [x] Erster Push auf `main` (öffentliches Repo); Refusal-Fallback entfernt, Whisper-Timing ist Standard
- [x] Schritt 3 (Code): Review-Bot, Upload, `auth_youtube.py`, systemd-Units + Installer; 26 Tests grün
- [x] Echter Testlauf (#104 Cyber Resilience Act): Skript gut, alle 8 Belege wörtlich gefunden, per Bot zugestellt.
  Bot läuft (`docker compose up -d bot`).
- [ ] **Offen: Videolänge.** Stimme `nova` spricht ~1,9 Wörter/s → 103 Wörter = 57 s statt 30–45 s.
  Entscheidung des Betreibers nötig (Wortbereich senken und/oder Tempo erhöhen).
- [ ] Whisper-Abgleich: Wortanzahl weicht oft ab (Zahlwörter, Bindestriche) → derzeit Schätzung im Sprechfenster;
  besser wäre Sequenz-Alignment (difflib) zwischen Skript- und Whisper-Wörtern.
- [ ] Betreiber: `sudo bash deploy/install_timers.sh`; `token_de.json` per scp
- [x] Hintergründe (generierte Animation, Pixabay-Anbindung) + Faktenkarten + Stimme +15 %; 37 Tests grün.
  Pixabay live getestet: Antwortformat passt, Cache/Drossel/Download-Limit greifen, Quellenangabe wird erfasst.
- [x] Telegram-Buttons end-to-end getestet (#104 vom Betreiber abgelehnt, dann `regenerate` → `neu`,
  wird beim nächsten Lauf bevorzugt mit Faktenkarten neu erzeugt).
- [x] API-Projekt **nicht auditiert** → Uploads bleiben privat; Upload-Meldung enthält Studio-Link
  (`https://studio.youtube.com/video/<id>/edit`). Offen: prüfen, ob gesperrte Videos sich in Studio überhaupt
  umstellen lassen (vermutlich nicht → ggf. manueller Upload-Modus).
- Lehre: Ein abgebrochener `docker compose run` läuft im Hintergrund weiter → parallele Läufe sind jetzt per
  `flock` auf `data/job.lock` gesperrt (pipeline + upload).
- [ ] YouTube-API-Audit beantragen (sonst bleiben Uploads privat)
- Offen/Ideen: eigene Hintergrundvideos in `assets/backgrounds/`, Piper als günstige TTS, englischer Kanal.
