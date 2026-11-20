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
| `manual.py` | **Standard (`UPLOAD_MODE=manual`)**: Termin je freigegebenem Video (gleiche Slot-Logik), am Termin-Tag Upload-Paket an Telegram (MP4 als Dokument, Titel, Beschreibung inkl. Pixabay-Credit, Tags, Termin + Button `up:<id>`), Status `paket_gesendet` → Button → `hochgeladen` |
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
- Stimme: `TTS_SPEED` **1.25** (ffmpeg **atempo** vor Whisper – im Hörvergleich gegenüber rubberband und
  Originaltempo bevorzugt; Tempo 1,25 im zweiten Vergleich gewählt), OpenAI liefert WAV (verlustfrei). Skript 60–75 Wörter (siehe Retention-Regeln).
- Testmodus (`--dry-run`, Demo-Pakete) nutzt espeak → klingt absichtlich roboterhaft; beim Versenden immer dazuschreiben.
- **Kein fremdes Gameplay** (urheberrechtlich geschützt). Erlaubt: eigene Aufnahmen, Pixabay, eigene Animation.

## Retention-Regeln (aus ersten Analytics: 74 % wischen weg, Ø 26 von 47 s)
- Hook: max. 8 Wörter, du-Ansprache, konkreter Nutzen/Verlust, keine Datums-Einleitung (`hook_problems()`),
  Rechtsstatus exakt (soll/kann/geplant bleibt erhalten), nicht mehr Betroffene versprechen als die Quelle.
- Skript 60–75 Wörter → 25–35 s. Erste Faktenkarte ab Frame 0, Titelbox klein ab 1,2 s, keine Endkarte
  (Quelle nur in der Beschreibung), letzte Karte bis zum letzten Frame → sauberer Loop.
- Kosten je Video in der Telegram-Review (`costs.py`): Claude exakt aus Tokens, TTS/Whisper geschätzt.

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

## Stand (2026-11-20 abends)
**Läuft:** Timer (Pipeline So 06:00, Upload täglich 09:30), Review-Bot (`docker compose up -d bot`),
`UPLOAD_MODE=api`, `TTS_SPEED=1.25`, Pixabay aktiv. 57 Tests grün, pyflakes sauber.

**YouTube ohne Audit – Beobachtungen:**
- API-Upload + von Hand in Studio öffentlich → von außen abspielbar (#1 Frühstartrente, Aufrufe vorhanden).
- #105 mit `publishAt` 17:00 war danach „nicht verfügbar“ – Ursache offen (gelöscht oder gesperrt?).
- Testvideo `opEuZX4jC-I`: 13:02 öffentlich, 13:32 „nicht verfügbar“ (evtl. vom Betreiber gelöscht).
- → `check-public` nach jedem Termin; bei gesperrten Videos `UPLOAD_MODE=manual`. Audit trotzdem beantragen.
- `upload --sofort` (direkt öffentlich) ist eingebaut, ohne Audit noch unerprobt.

**Offen / Ideen:**
- Whisper-Abgleich per Sequenz-Alignment (Wortzahl weicht oft ab → derzeit Schätzung im Sprechfenster).
- Untertitel zeigen Zahlen ausgeschrieben ("ZWEITAUSENDZWANZIG") – ggf. Ziffern anzeigen.
- #104 (alte Fassung) wartet noch auf Freigabe → besser `regenerate 104`.
- Webinterface (lokal, Passwort beim ersten Start) – vom Betreiber angefragt, noch nicht gebaut.
- Englischer Kanal, Piper als günstige TTS, eigene Hintergrundclips.

**Lehren:** Ein abgebrochener `docker compose run` läuft im Hintergrund weiter → `flock` auf `data/job.lock`.
Vor Aussagen über YouTube-Sichtbarkeit immer von außen prüfen (gelöscht ≠ gesperrt ≠ privat).
