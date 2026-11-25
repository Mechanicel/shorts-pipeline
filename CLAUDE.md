# CLAUDE.md – shorts-pipeline

## Ziel
Automatisierte YouTube-Shorts für den Kanal **„Was ändert sich für dich?“**: ca. 25 s Hochkant-Videos über
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
| `db.py` | SQLite (`data/pipeline.sqlite3`, WAL) + Status-Maschine mit erlaubten Übergängen, additive Migrationen, `app_settings`, `job_runs`, `upload_counts` |
| `sources.py` | RSS (feedparser) + HTML-Linklisten (Regex), URL-Normalisierung, Artikeltext via trafilatura |
| `triage.py` | Vorauswahl vor dem teuren Modell: Vorfilter per `title_blocklist` (kostenlos), dann Claude Haiku 5.5 (`ANTHROPIC_TRIAGE_MODEL`, ein Aufruf für bis zu `TRIAGE_MAX`=30 Themen, nur Titel+Anriss, im Zweifel durchlassen). Erkennt Dubletten zu Themen der letzten 30 Tage (`duplicate_of`) und vergibt Priorität 1–5 (`triage_score`, Opus-Reihenfolge). `similar_scripts()` = KI-freie Dubletten-Warnung auf fertigen Skripten. Nur `ok`-Themen gehen an Opus |
| `script.py` | Claude (`messages.parse` + Pydantic `ShortScript`, Modell/Effort aus `.env`; kein Refusal-Fallback – Ablehnung = Thema verworfen). Validierung 55–68 Wörter, Hook ≤ 8 Wörter, 2. Satz ohne Behörden-Einstieg (`hook_problems()`), 1 Korrekturversuch; bekommt die Themen der letzten 30 Tage (Dubletten). Belege (`evidence`) mit wörtlichen Zitaten → `unverified_quotes()` prüft gegen Quelltext |
| `tts.py` | `TTSEngine`-Protocol; `OpenAITTS`, `EspeakTTS` (Dry-Run). Piper/ElevenLabs: neue Klasse + `get_tts()` |
| `subtitles.py` | Wort-Timings (Standard: Whisper-Wortzeitstempel; `SUBTITLE_TIMING=estimate` als kostenlose Schätzung), Chunking, ASS-Datei (Titel, Untertitel mit gelbem aktivem Wort, Endkarte mit Quelle) |
| `backgrounds.py` | Hintergrund: eigene Clips (`assets/backgrounds/`) → Pixabay-Pool (`data/backgrounds/pixabay/`) → generierte Ball-Physik-Animation (`data/backgrounds/generated/`, 6 Varianten, gecacht) → Farbverlauf. Liefert `BackgroundChoice(path, credit)` |
| `graphics.py` | Faktenkarten-PNGs im Kanal-Design (Navy `#0B1F3A`, Gelb `#FFD23F`, Montserrat): Datums-Pill + große Zahl + Label, gleichmäßig über die Sprechzeit verteilt |
| `render.py` | ffmpeg: 1080x1920, Hintergrund auf 9:16 zugeschnitten + abgedunkelt + weichgezeichnet, Faktenkarten-Overlay, ASS eingebrannt, loudnorm −14 LUFS |
| `pipeline.py` | Orchestrierung collect → script → render, Fehler pro Thema → Status `fehler`, Zusammenfassung per Telegram |
| `notify.py` | Telegram-`sendMessage` für Fehler/Status (no-op ohne Konfiguration) |
| `review_bot.py` | Telegram-Bot (python-telegram-bot, JobQueue pollt alle 60 s Status `video`): Video+Skript+Quelle+Belege, Buttons Freigeben/Ablehnen, nur `TELEGRAM_CHAT_ID` |
| `manual.py` | **Standard (`UPLOAD_MODE=manual`)**: Termin je freigegebenem Video (gleiche Slot-Logik), am Termin-Tag Upload-Paket an Telegram (MP4 als Dokument, Titel, Beschreibung inkl. Pixabay-Credit, Tags, Termin + Button `up:<id>`), Status `paket_gesendet` → Button → `hochgeladen` |
| `upload.py` | YouTube Data API v3, privat + `publishAt` auf `publish_slots` (Berlin) verteilt oder `--sofort`; Googles `quotaExceeded`/`uploadLimitExceeded` stoppen den Batch (Sperre bis Mitternacht PT), keine eigene Vorab-Rechnung; Token-Refresh schreibt `credentials/token_<key>.json` zurück |
| `research.py` | Ausreißer-Recherche (CLI `research`, Seite „Recherche“): Shorts kleiner DE-Kanäle über die Data API (`YT_API_KEY`, kein OAuth, kein Scraping), Score = Aufrufe / Median der letzten 20 Uploads, Tabellen `research_*`, Auswertung mit Sonnet 5.5, Bericht + CSV in `docs/research/`, manuelle Sichtung im Web. Konfiguration `config/research.yaml` |
| `merkliste.py` | **Stil v3 „Merkliste“**: Skriptmodell (headline, 3–6 Punkte mit Icon/alt→neu/Status/Beleg, CTA „Schick das jemandem, der …“), Prompt, Prüfungen (Textlängen, Layout passt auf die Tafel, 18–28 Wörter, Status vs. Beleg, Kopfzeile „neu“ nur mit echten Änderungen), Tafel 1080×1920 (min. 42 px, Lucide-Icons ISC in `assets/icons/`), 8–12 s, statischer unscharfer Hintergrund (Pixabay-Foto zum Suchbegriff von Claude, sonst Navy-Verlauf) → exakter Loop |
| `deadline.py` | Fristen/Stichtage aus `date_label` → Video erscheint ≥ 3 Tage vorher, sonst nicht eingeplant + „sofort“ vorgeschlagen; Vorrats-Prüfung (< 3 Tage geplant → Telegram, täglich mit check-public) |
| `analytics.py` | YouTube Analytics API (`yt-analytics.readonly`, Token mit beiden Scopes via `auth_youtube.py`): täglich views/engagedViews/Ø-Dauer/Ø-%/Likes/Kommentare/Shares in `yt_stats`, Aufrufe nach 72 h in `topics.yt_views_72h`, Stil-Vergleich im Dashboard. „Angesehen vs. weggewischt“ liefert die API NICHT → Eingabefeld (`topics.yt_viewed_rate`) |
| `actions.py` | Freigeben/Ablehnen/`regenerate` für Bot, CLI und Web (entfernt alte Telegram-Buttons) |
| `publiccheck.py` | `check-public`: Sichtbarkeit von außen (oEmbed + playabilityStatus) |
| `web/` | Webinterface (FastAPI, Port 8080, Dienst `web`): Seiten Heute · Freigabe · Videos · Statistik · Recherche · Jobs · Einstellungen (je eine Aufgabe), **Kanal pro Anfrage** (Cookie `kanal`, Umschalter oben links ab 2 Kanälen, `current(request)` statt festem Kanal, ein Scheduler je Kanal), `views.py` = testbare Aufbereitung (Reiter, Zeitleiste, Klartext-Fehler, SVG-Diagramm), Passwort beim ersten Start (scrypt), Login-Sperre; **Zeitplan** (APScheduler, `schedule_<kanal>` in `app_settings`) startet CLI-Jobs als Subprozess (`job_runs`, Logs in `data/logs/`). **Design: `docs/ui/DESIGN.md` verbindlich** |
| `__main__.py` | CLI: `pipeline [--dry-run] [--limit N] [--style v2|v3]`, `collect`, `status`, `upload [--sofort] [--only ID]`, `check-public`, `research`, `regenerate <id> [--style v2|v3]`, `retry`, `auth-check`, `refresh-dates`, `bot`, `web`; flock auf `data/job.lock` |

Status: `neu → skript → video → wartet_auf_freigabe → freigegeben → hochgeladen`; Nebenwege `verworfen`
(Claude/zu kurz/veraltet >21 Tage), `abgelehnt` (Betreiber), `fehler` (retrybar via `retry`).

Dry-Run: eigene DB `data/dryrun.sqlite3` + `output/dryrun/`, wird pro Lauf zurückgesetzt; Beispieldaten in
`sample_data.py` (fiktiv, Video mit „TESTVIDEO“-Badge); espeak statt OpenAI, kein Claude-Aufruf.

Außerdem: `auth_youtube.py` (Repo-Root, läuft auf dem Laptop des Betreibers), `deploy/setup_host.sh` (Host-Einrichtung),
`deploy/ui_check.sh` (UI-Prüfung).

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
  Originaltempo bevorzugt; Tempo 1,25 im zweiten Vergleich gewählt), OpenAI liefert WAV (verlustfrei). Skript 55–68 Wörter (siehe Retention-Regeln).
- Testmodus (`--dry-run`, Demo-Pakete) nutzt espeak → klingt absichtlich roboterhaft; beim Versenden immer dazuschreiben.
- **Kein fremdes Gameplay** (urheberrechtlich geschützt). Erlaubt: eigene Aufnahmen, Pixabay, eigene Animation.

## Retention-Regeln (aus ersten Analytics: 74 % wischen weg, Ø 26 von 47 s)
- Hook: max. 8 Wörter, du-Ansprache, konkreter Nutzen/Verlust, keine Datums-Einleitung (`hook_problems()`),
  Rechtsstatus exakt (soll/kann/geplant bleibt erhalten), nicht mehr Betroffene versprechen als die Quelle.
- Skript 55–68 Wörter → ≈ 25 s. Hook als große Tafel ab Frame 0 (Gelb auf Navy) bis Satzende (mind. 1,6 s),
  währenddessen keine Wort-Untertitel/Titelbox; danach Faktenkarten, letzte bis zum letzten Frame → Loop.
  Zweiter Satz liefert sofort die Kernänderung (kein „Die Bundesregierung …“-Einstieg, `hook_problems()`).
  Keine Endkarte (Quelle nur in der Beschreibung). Stil-Version steht als `style` im script_json (`SCRIPT_STYLE`).
- Analytics 22.11. (8 Shorts): nur 14–29 % „angesehen statt weggewischt“, Abfall bei ~4,5–5 s (nach dem Hook),
  Ø ~50 % gesehen. Beste: 116 117 (Neugier-Hook „Diese Nummer …“), Unterhaltsvorschuss, DAZN. Publikum v. a. 55+.
  Doppelte Stromsteuer-Videos bekamen ~0 Reichweite → Dubletten-Schutz. Nach ~2 Wochen v1 vs. v2 vergleichen.
- Kosten je Video in der Telegram-Review (`costs.py`): Claude exakt aus Tokens, TTS/Whisper geschätzt.

## Webinterface-Design (verbindlich, Details in `docs/ui/DESIGN.md`)
- Jede Seite hat eine Aufgabe; „Heute“ passt bei leerer „Braucht dich“-Box auf 1440×900. Neue Inhalte auf die
  passende Seite, nicht auf Heute.
- Ruhige Flächen (grau-blaue Neutraltöne, Linien 8 %), Primärknopf Navy, **Gelb nur als Textmarker** (aktiver
  Menüpunkt, nächster Termin, laufender Job). Raster 4 px, Radien 6/8/12, Gewichte 400/500, Systemschrift.
- Status nur über `_macros.html` (`badge`, `law`, `job_badge`, `style`), Datum nur über `| dt` („Mo 23.11., 17:00“),
  relative Angaben (`| rel`) nur als grauer Zusatz. Keine Emojis, keine Ausrufezeichen, kein Punkt am Ende von Labels.
- Fehler in Klartext (`views.humanize_error`), technisches Detail aufklappbar. Zahlen rechtsbündig mit tabular-nums.
- Mobil: keine horizontale Scrollleiste, Tabellen → Karten (`table.compact`, `td.inline`).
- Nach UI-Änderungen `./deploy/ui_check.sh` laufen lassen (Screenshots in `docs/ui/`, prüft Höhe, Scrollleisten,
  Konsolenfehler). Entstanden mit dem Skill unslop (@heydamir, CC BY 4.0).

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
- YouTube: Laut Doku werden Uploads ungeprüfter API-Projekte privat; bei uns wurden `publishAt` und `--sofort` trotzdem
  öffentlich (von außen geprüft) – weiter mit `check-public` beobachten, Audit offen. OAuth-Consent auf „In Produktion“,
  sonst läuft das Refresh-Token nach 7 Tagen ab.
- Kontingent-Modell: `videos.insert` und `search.list` haben je einen eigenen Topf von 100 Aufrufen/Tag; übrige Endpunkte
  teilen sich 10.000 Einheiten (Reset Mitternacht Pacific Time). Maßgeblich sind Googles Fehlermeldungen, nicht eigene Zähler.
- Verbraucherzentrale hat kein RSS → `type: html` mit `link_pattern` (Startseite, eher Ratgeber als News).
- Bundesregierung-Feeds: `/service/rss/breg-de/<id>/feed.xml` (1151246 Artikel, 1151244 Pressemitteilungen).
- feedparser liefert UTC-`struct_time` → `calendar.timegm`, nicht `mktime` (Container-TZ ist Berlin).

## Stand (2026-11-25)
**Zeitsteuerung:** nur noch der Zeitplan im Webinterface (APScheduler, `schedule_<kanal>` in `app_settings`).
Keine systemd-Timer mehr (Units am 25.11. aus dem Repo entfernt, auf dem Host deaktiviert). Aktiv: Sichtbarkeitsprüfung täglich 12:00.
Erstellen und Upload sind im Web aus → der Betreiber startet sie von Hand. Dienste: `bot`, `web` (Port 8080).
`UPLOAD_MODE=api`, `TTS_SPEED=1.25`, Pixabay aktiv. Statusbericht: `docs/STATUS.md`.

**YouTube ohne Audit – Beobachtungen (20.11.):**
- Funktioniert, von außen geprüft: von Hand veröffentlichen (#1), `publishAt` (#619, 19:30 → 19:31 öffentlich),
  `--sofort` (#622). #105 und ein Testvideo waren später „nicht verfügbar“ (vermutlich vom Betreiber gelöscht).
- → `check-public` nach Terminen weiter beobachten; bei Sperren `UPLOAD_MODE=manual`. Audit trotzdem beantragen.

**Themenauswahl (seit 20.11. abends):** Ablauf Vorfilter → Haiku-Vorauswahl → Datumsprüfung → Opus-Skript.
Seit 22.11.: Dubletten-Schutz (URL-Dedupe reichte nicht – mehrere Artikel zum selben Kabinettspaket ergaben
4× Stromsteuer). Haiku und Opus sehen die Liste der Themen der letzten 30 Tage; Opus verwirft mit
„Dublette von #…“; Skripte mit ähnlichen Inhaltswörtern bekommen einen Warnhinweis.
Probelauf: 30 Themen für 0,12 Cent, 19 durchgelassen. Leere Läufe melden „Kein Video: N Themen geprüft und
verworfen (2× Vorfilter, …)“ plus Begründungen im Job-Log.

**Kontingent (seit 20.11. abends):** videos.insert hat laut Google-Doku (Stand 12.11.2026) einen eigenen
Topf von 100 Aufrufen/Tag (Console zeigte 6/100), nicht 1.600 von 10.000 Einheiten. Die App blockiert NICHT
mehr vorab: Uploads werden versucht, `quotaExceeded`/`uploadLimitExceeded` stoppen den Batch und speichern
eine Sperre bis Mitternacht PT (`upload_block` in app_settings; Timer-Läufe mit `--scheduled` pausieren und
melden einmal, manuell geht weiter). Rate-Limits/5xx: 3 Versuche mit Backoff. Eigene Zählung
(`upload_counts`, `YT_UPLOADS_PER_DAY`) nur zur Anzeige. Termin wird erst nach erfolgreichem Upload gespeichert.

**Webinterface:** läuft (`docker compose up -d web`), Passwort legt der Betreiber beim ersten Aufruf fest.

**Stil v3 / Vergleich (seit 25.11.):** `STYLE_MODE=alternate` (Standard) gibt dem selteneren Stil den Vorzug →
v2 und v3 gleich oft, an wechselnden Wochentagen; Stil in `topics.style`. Von Hand gewählt (Web, `--style`) →
`topics.style_pick`, hat Vorrang vor `STYLE_MODE`; `regenerate` ohne Stil setzt ihn zurück.
Fest gewählt v3, aber keine Liste möglich → **kein Ersatz-Video als v2**: Lauf-Wahl lässt das Thema offen
(Notiz „v3 nicht möglich:“, wird in weiteren v3-Läufen übersprungen), Thema-Wahl → `fehler`. Ergebnis „Kein Video: …“. v3 nicht möglich (< 3 listenfähige
Fakten oder nur alte Regeln) → Grund in der Notiz, Thema läuft als v2. Vorschau ohne Upload/echte DB:
`docker compose run --rm job preview <URL> [--only v2|v3]` → Telegram. Anthropic-Clients mit `timeout=240`
(eine hängende Verbindung blockierte sonst 10 min). Quellen: „Bundesregierung – Neuregelungen“ (Hub-Seite der
Monatsübersichten) neu; Pressemitteilungen, BMF, BNetzA mit `enabled: false` (0 Videos aus 57 Themen).

**Ausreißer-Recherche (seit 25.11.):** `docker compose run --rm job research --terms 10`. Nur Data API v3 mit
`YT_API_KEY`; max. 40 search-Aufrufe/Lauf; Kanal-Median 7 Tage gecacht; jeder Lauf schreibt Snapshots (Wachstum).
Filter u. a. Kanal-Median ≥ 300 (sonst absurde Scores bei Repost-Kanälen); danach sortiert Haiku Politik/Satire/
Unterhaltung aus (Spalte `relevant`, gecacht). `research --reanalyze` wertet ohne search-Kontingent neu aus.
Titelmuster/Längen/Kanäle werden im Code exakt gezählt, Claude (Sonnet 5.5, effort medium) deutet nur und liefert
Cluster + 10 Ideen. Manuelle Sichtung der ersten 3 s im Web; ab 15 Sichtungen „Muster auswerten“.

**Offen / Ideen:**
- Whisper-Abgleich per Sequenz-Alignment (Wortzahl weicht oft ab → derzeit Schätzung im Sprechfenster).
- Untertitel zeigen Zahlen ausgeschrieben ("ZWEITAUSENDZWANZIG") – ggf. Ziffern anzeigen.
- Englischer Kanal, Piper als günstige TTS, eigene Hintergrundclips.

**Lehren:** Ein abgebrochener `docker compose run` läuft im Hintergrund weiter → `flock` auf `data/job.lock`.
Nach Änderungen an `db.py`/Code, den `bot` und `web` dauerhaft geladen haben: beide neu starten
(`docker compose restart bot web`) – aber nur, wenn kein Job läuft (Web-Neustart beendet laufende Jobs; verwaiste
Einträge markiert der JobRunner beim Start als abgebrochen). `Topic.from_row` ignoriert unbekannte Spalten.
Vor Aussagen über YouTube-Sichtbarkeit immer von außen prüfen (gelöscht ≠ gesperrt ≠ privat).
