# Statusbericht shorts-pipeline

Stand: 25.11.2026, geprüft an Code, git, SQLite-DB, Docker und Job-Logs auf dem Server. Kanal: „Was ändert sich für dich?“ (DE).

## Nachtrag 25.11.2026 nachmittags (nach diesem Bericht umgesetzt)

| Thema | Stand | Commit |
|---|---|---|
| Fristen bei der Terminvergabe | Video mit Frist/Stichtag erscheint ≥ 3 Tage vorher; sonst nicht eingeplant, „sofort“ wird vorgeschlagen (Web vorausgewählt, Telegram-Warnung) | d36fbb9 |
| Vorrats-Warnung | täglich 12:00 mit check-public: < 3 Tage geplant → Telegram mit Anzahl und letztem Termin | d36fbb9 |
| Ausreißer-Recherche | `research` (Data API, `YT_API_KEY`), Mindest-Median 300, Haiku sortiert Politik/Satire aus, Seite „Recherche“ mit Sichtung | b1a419d, 7c1f9d0 |
| **Stil v3 „Merkliste“** | Infotafel mit 3–6 Punkten (alt → neu, Status-Chip, Beleg), 8–12 s, CTA, ab Frame 0 sichtbar; Layout-Prüfung mit bis zu 2 Korrekturen | 6e8ca28 |
| Vergleich v2/v3 | `STYLE_MODE=alternate` (Standard), Stil in `topics.style`, Tabelle „Stil-Vergleich“ (Videos, Ø Aufrufe nach 72 h, Ø Länge) | 6e8ca28 |
| Analytics API | `analytics` + täglich: views, engagedViews, Ø-Dauer, Ø-%, Likes, Kommentare, Shares. „Angesehen vs. weggewischt“ ist nicht in der API → Eingabefeld pro Video. **Token mit Analytics-Scope steht noch aus.** | 6e8ca28 |
| Quellen | neu: Bundesregierung – Neuregelungen (Monatsübersichten); deaktiviert: Pressemitteilungen, BMF, BNetzA | 6e8ca28 |
| Vorschau | `preview <URL>` erzeugt v2 und v3 ohne Upload/echte DB; erste Vorschau: Altersvorsorgedepot ab 2027 | dieser Commit |

Die Abschnitte 4–8 unten beschreiben den Stand vor diesen Änderungen (v2-Prompt wörtlich, Zahlen vom Vormittag).
Der v3-Prompt steht in `src/shorts/merkliste.py` (`V3_SYSTEM`).

## 1. Kurzfazit

Die Pipeline läuft durch: Quellen → Vorauswahl → Skript (Opus) → Stimme → Video → Freigabe → YouTube-Upload mit Termin. Bisher sind 22 Videos hochgeladen, davon 10 öffentlich, 6 geplant und 6 gelöscht (Dubletten bzw. veraltet). Automatisch läuft derzeit nur die tägliche Sichtbarkeitsprüfung. Erstellen und Hochladen startet der Betreiber von Hand im Webinterface, weil die Zeitpläne für Erstellen und Upload aus sind. Das größte inhaltliche Problem ist die Zuschauerbindung: Laut Studio sahen bei den ersten 8 Shorts nur 14–29 % hin, statt wegzuwischen. Seit 22.11. wird die Gestaltung v2 dagegen getestet (Hook-Tafel ab Frame 0), Auswertung steht aus. Das größte praktische Risiko: Der Vorrat an geplanten Videos reicht nur bis 30.11.

## 2. Letzte Änderungen (git, letzte 14 Tage)

Alle 20 Commits stammen vom 20.–22.11.2026.

| Thema | Inhalt | Letzter Commit |
|---|---|---|
| Grundgerüst | Docker, SQLite-Statusmaschine, Dry-Run, Review-Bot (Telegram), YouTube-Upload, systemd-Timer, Sperre gegen Doppelläufe | 533599b (20.11.) |
| Gestaltung & Stimme | Pixabay-Hintergründe mit Tag-Filter, Faktenkarten im Kanal-Design, TTS als WAV, Tempo 1,25 | fd09e4d (20.11.) |
| Retention v1 | Hook max. 8 Wörter, 25–35 s, Kernzahl ab Frame 0, keine Endkarte, Kosten pro Video | 7dc77b9 (20.11.) |
| Upload-Modi | manuell/API, `--sofort`, `--mode`, Studio-Link, Video-ID im Terminal | ca45e31 (20.11.) |
| Webinterface | FastAPI mit Passwort, Zeitplan-Steuerung, Job-Verlauf | 3d187aa (20.11.) |
| Testbericht P0/P1 | ehrliche Job-Ergebnisse, Themen-Datum, YouTube-Sync, Freigabe-Karte, Einzel-Upload, Mobil | 0849be9 (20.11.) |
| Kontingent | Googles Fehler `quotaExceeded`/`uploadLimitExceeded` entscheiden, keine eigene Vorab-Rechnung | d436887 (20.11.) |
| Themenauswahl | Vorfilter (Titel-Blockliste) und Vorauswahl mit Claude Haiku vor dem Opus-Skript | 13d59d5 (20.11.) |
| Analytics-Reaktion (v2) | Dublettenschutz, Hook-Tafel ab Frame 0, zweiter Satz mit der Kernänderung, 55–68 Wörter, Priorität 1–5 | 9b6e2c4 (22.11.) |

- **HEAD:** `9b6e2c4` auf `main`, identisch mit `origin/main`, alles gepusht. Dieser Bericht ist danach der nächste Commit.

## 3. Architektur in Kürze

### Module (`src/shorts/`)

| Modul | Aufgabe |
|---|---|
| `__main__.py` | CLI: pipeline, collect, status, upload, check-public, regenerate, retry, auth-check, refresh-dates, bot, web; flock-Sperre |
| `config.py` | Einstellungen aus `.env` und Kanäle aus `config/channels.yaml` |
| `db.py` | SQLite (WAL), Statusmaschine mit erlaubten Übergängen, additive Migrationen, Jobs, Einstellungen, Upload-Zähler |
| `sources.py` | RSS- und HTML-Linklisten einlesen, URLs normalisieren, Artikeltext und -datum per trafilatura |
| `triage.py` | Vorfilter (Blockliste), Haiku-Vorauswahl (relevant, Dublette, Priorität), Dublettenwarnung ohne KI |
| `script.py` | Opus-Skript per Structured Output, Prüfungen (Wortzahl, Hook, 2. Satz, Belegzitate, Grafik-Zahlen) |
| `tts.py` | OpenAI-TTS (WAV), espeak nur für Dry-Run, Tempo per ffmpeg atempo |
| `subtitles.py` | Wort-Timings (Whisper oder Schätzung), ASS-Untertitel, Hook-Tafel, Titelbox |
| `graphics.py` | Faktenkarten als PNG (Navy/Gelb/Montserrat) |
| `backgrounds.py` | Hintergrund: eigene Clips → Pixabay (Cache, Drossel) → generierte Animation |
| `render.py` | ffmpeg-Render 1080×1920, Overlays, Lautheit −14 LUFS |
| `pipeline.py` | Ablauf collect → script → render → review, Laufbericht |
| `review_bot.py` | Telegram: Review-Nachricht mit Video, Freigeben/Ablehnen/Hochladen |
| `actions.py` | gemeinsame Freigabe-, Ablehnungs- und Neuerzeugungslogik für Bot, CLI und Web |
| `upload.py` | Termine, Metadaten, YouTube-Upload mit Backoff, Kontingentsperre |
| `manual.py` | manueller Modus: Upload-Paket per Telegram |
| `publiccheck.py` | Sichtbarkeit von außen (oEmbed/playability): öffentlich, privat oder gelöscht → DB |
| `costs.py` / `result.py` / `notify.py` | Kosten pro Video, strukturierte Job-Ergebnisse, Telegram-Text |
| `web/` | FastAPI-UI (Login, Übersicht, Einstellungen, Jobs), APScheduler-Zeitplan, Job-Runner |

### Ablauf eines Durchgangs

1. **collect:** RSS- und HTML-Quellen lesen → neue URLs als `neu`. Dubletten erkennt dieser Schritt nur an der URL.
2. **Vorfilter:** Titel enthält einen Begriff der Blockliste → `verworfen`. Das kostet nichts.
3. **Vorauswahl:** Haiku prüft bis zu 30 Themen in einem Aufruf, mit Titel und Anriss sowie der Liste schon behandelter Themen. Irrelevante Themen und Dubletten → `verworfen`; die übrigen bekommen `triage="ok: …"` und `triage_score` 1–5.
4. **Datum:** Artikeltext und Datum holen; ohne Datum oder älter als 21 Tage → `verworfen`.
5. **Skript (Opus):** Die Themen kommen nach Priorität dran. Verwirft das Skript ein Thema oder ist es ungültig → `verworfen` bzw. `fehler`. Sonst `neu → skript`.
6. **Render:** TTS, Tempo 1,25, Whisper-Timings, Hintergrund, ffmpeg → `skript → video`.
7. **Review:** Telegram-Nachricht → `video → wartet_auf_freigabe`. Freigeben geht auch direkt aus `video` im Web.
8. **Freigabe:** → `freigegeben`; mit „Ablehnen“ → `abgelehnt`. Die Freigabe lässt sich zurücknehmen (→ `wartet_auf_freigabe`).
9. **Upload (API):** nächster freier Termin, privat mit `publishAt` oder sofort öffentlich → `hochgeladen`. Im manuellen Modus wird stattdessen ein Paket verschickt → `paket_gesendet`.
10. **check-public:** täglich; schreibt `yt_visibility` (öffentlich/privat/gelöscht) und den echten Veröffentlichungszeitpunkt. „Gelöscht“ ist kein eigener Status, sondern ein Wert von `yt_visibility`.

### Dienste und Zeitpläne

| Was | Zustand |
|---|---|
| Container `bot` (Telegram) | läuft (Up 2 days) |
| Container `web` (Port 8080) | läuft (Up 2 days) |
| Container `job` | nur für Einmal-Läufe (profile jobs) |
| systemd `shorts-pipeline.timer`, `shorts-upload.timer` | installiert, **deaktiviert** |
| Web-Zeitplan „Erstellen“ | **aus** (eingestellt: So 06:00, 5 Videos) |
| Web-Zeitplan „Upload“ | **aus** (eingestellt: täglich 09:30) |
| Web-Zeitplan „Sichtbarkeit prüfen“ | an, täglich 12:00 (nächster Lauf 26.11. 12:00) |
| Veröffentlichungstermine | Mo–Fr 17:00, Sa/So 11:00 (Europe/Berlin) |

## 4. Inhaltliche Logik

### Quellen

| Quelle | Typ | Datum | Themen bisher / davon online |
|---|---|---|---|
| Bundesregierung – Artikel | RSS | ja (pubDate) | 32 / 11 |
| Bundesregierung – Pressemitteilungen | RSS | ja | 26 / 0 |
| Bundesfinanzministerium – Pressemitteilungen | RSS | ja | 20 / 0 |
| BMAS – Aktuelles | RSS | ja | 20 / 0 (1 hochgeladen, als Dublette gelöscht) |
| Bundesnetzagentur – Pressemitteilungen | RSS | meist (1 ohne) | 11 / 0 |
| Verbraucherzentrale – Wissen | HTML-Linkliste | von der Artikelseite (trafilatura), 2 ohne | 15 / 5 |

**Altersgrenze:** Quelle älter als 21 Tage oder ohne Datum → verworfen, bevor Opus Geld kostet. Zusätzlich verwirft Opus Änderungen, die schon über 4 Wochen in Kraft sind.

### Themenauswahl

- **Vorfilter, kostenlos:** Die Blockliste in `channels.yaml` wird mit dem Titel abgeglichen; ein Begriff zählt nur am Wortanfang. Begriffe: gratuliert, kondoliert, Glückwunsch, empfängt, Treffen mit, trifft, telefoniert, Telefonat, Reise nach, reist nach, besucht, Antrittsbesuch, Staatsbesuch, Tagung, Konferenz, Rede von, würdigt, Gedenken.
- **Vorauswahl mit Haiku:**
  - Modell `claude-haiku-5-5`, effort low, ohne Thinking.
  - Ein Aufruf prüft bis zu 30 Themen und kostet rund 0,1–0,2 Cent.
  - Sie entscheidet: `relevant` (konkrete Änderung für Privatpersonen, im Zweifel ja), `duplicate_of` (gegen die Themen der letzten 30 Tage und innerhalb des Batches) und `score` 1–5.
  - 5 Punkte bekommen Themen, die viele Menschen direkt betreffen: Geld, Gesundheit, Rente, Familie, Frist, Geld zurück.
  - Fehlt eine Einschätzung oder fällt die Vorauswahl aus, geht das Thema durch.
- **Opus:** bekommt dieselbe Liste behandelter Themen. Ist der Kern schon behandelt, verwirft Opus mit „Dublette von #…“.
- **Warnung ohne KI:** Teilt ein fertiges Skript mindestens 2 Inhaltswörter (≥ 30 % Überlappung) mit einem vorhandenen, erscheint „Mögliche Dublette“ im Web und in Telegram.
- **Reihenfolge:** Neu erzeugte Themen zuerst, dann nach Priorität. Höchstens 12 Opus-Aufrufe pro Lauf (`max_new_topics_per_run`), höchstens 7 Skripte (`max_videos_per_run`).

### Skript-Prompt (wörtlich aus `src/shorts/script.py`)

- **Wortgrenzen:** 55–68 Wörter (`MIN_WORDS`, `MAX_WORDS`).
- **Hook:** höchstens 8 Wörter.
- **Stil-Version:** `v2-hook-frame0`, wird pro Video im script_json gespeichert.
- **Prüfung im Code** (`validate_script`): Wortzahl, Hook-Länge, du-Ansprache im Hook, keine Datums-Einleitung, der 2. Satz beginnt nicht mit Behörde oder Verfahren, Belege vorhanden, 1–3 Kernzahlen, Längen der Grafiktexte. Bei Problemen gibt es einen Korrekturversuch, danach `fehler`.
- **Nach dem Skript:** Belegzitate und Grafik-Zahlen werden gegen den Quelltext geprüft; Abweichungen landen als Warnung im Review.
- **Aufruf:** `messages.parse` mit Modell `claude-opus-5-5`, effort high, max_tokens 16000. Eine Ablehnung durch das Modell verwirft das Thema (kein Fallback).

System-Prompt (Platzhalter: channel_name, locale = „Deutschland“, language = de, min_words 55, max_words 68, hook_max 8):

```text
You write scripts for the faceless YouTube Shorts channel "{channel_name}". Audience: ordinary people in {locale}. Every short answers one question: what changes for me, when, and what do I have to do? Write everything viewers see or hear in language "{language}".

Hard rules, in this order of priority:
1. Use only facts that are explicitly stated in the SOURCE TEXT. No outside knowledge, no assumptions, no rounded or extrapolated numbers, no invented dates. If the text is vague, the script stays vague or the topic is discarded.
2. Discard the topic (discard=true, with a short reason) if the core change is already covered by a topic in ALREADY COVERED and this source adds no substantial new fact for viewers; then discard_reason starts with "Dublette von #<id>". Also discard if any of these apply: the change already took effect more than 4 weeks before TODAY and the source reports nothing new about it (old news); no concrete change, deadline, price, amount or right for private individuals; purely political/diplomatic news; the decisive facts (what/when/who) are missing or uncertain; the text is only a teaser.
3. Every number, date, amount, deadline or rule in the narration needs an evidence entry whose quote is copied verbatim from the source text.

Script shape (when not discarded):
- The narration is {min_words}–{max_words} words (about 25 seconds spoken). Cut everything that is not essential.
- The first sentence is the hook. Most viewers decide in the first second whether to swipe away, so:
  * max. {hook_max} words,
  * talks to the viewer as "du" (du/dein/dir/dich),
  * does one of two things: names a concrete gain or loss for the viewer (money, a right, a deadline they could miss), or opens a curiosity gap around a concrete thing the viewer knows ("Diese Nummer soll dir bald helfen.", "Bei deiner Stromrechnung ändert sich etwas.") that the next sentence closes,
  * is readable at a glance: it is shown as large text on the first frame, so no filler words,
  * does NOT open with a date or time phrase ("Ab dem 1. Januar ...", "2027 ...", "Seit heute ..."); the date comes later in the script,
  * no "In diesem Video", no question the source cannot answer.
- The hook must be factually exact about the legal status: planned, proposed, decided by the cabinet, passed by parliament and in force are different things. Never present a plan or draft as decided or in force (e.g. "soll kommen" / "plant" instead of "kommt" when it is only planned). If the source says "soll/sollen/kann/geplant", the hook keeps that meaning ("soll ... bekommen", "bekommt bald").
- The hook must not promise more than the source: if only some people are affected (age, income, birth year, region), it must not sound like everyone is (e.g. "Kinder ab sechs" or "viele Kinder", not "dein Kind" when not every child qualifies). Short and exact beats catchy and wrong.
- The second sentence delivers the payoff immediately: the core change with its most important number. Viewers drop off right after the hook, so no background first: it must NOT start with the institution or procedure ("Die Bundesregierung ...", "Das Kabinett ...", "Der Bundestag ...", "Laut ...", "Wie ... mitteilt"). Put the legal status inside the sentence instead ("Geplant ist ...", "... soll ... sinken, so der Entwurf").
- Then: from when, who is affected, what to do. Short spoken sentences, numbers written as people say them, no abbreviations the voice would stumble over, no emojis, no URLs.
- Close with one short takeaway sentence that leads naturally back to the hook (the video loops). No outro, no "Quelle", no request for likes or subscriptions.
- The title is factual and specific and promises nothing the narration does not deliver: if the title says "so schaltest du X ab", the narration must explain exactly that. overlay_title is a few punchy words.

On-screen graphic (shown large in the foreground, so it must be exact):
- key_facts: the 1–3 most important numbers, in the order they are spoken. value uses digits and units exactly as in the source (e.g. "30 Tage", "4,2 %", "49 €"); label says in a few words what it is.
- date_label: the effective date or deadline exactly as stated in the source, in capitals (e.g. "AB 1. MÄRZ 2027", "BIS ENDE 2027"). Empty string if the source names none. Never guess a year.
- Do not repeat the date_label as a key fact; key_facts are amounts, durations, percentages or counts. If the date is the only number, give one key fact that names what happens (e.g. value "Pflicht", label "für neue Smart-Geräte") or leave key_facts with the single date-free number that matters most.
- Both follow rule 1: only what the source text states.
```

User-Prompt (`covered` = Liste „[#id] Titel – Kernzahlen“ der letzten 30 Tage):

```text
TODAY: {today}
ALREADY COVERED ON THE CHANNEL (last 30 days):
{covered}

SOURCE ({source_name}, published {source_date}): {url}
HEADLINE: {headline}

SOURCE TEXT:
<<<
{text}
>>>
```

Felder der strukturierten Ausgabe: discard, discard_reason, title (max. 70 Zeichen), overlay_title (max. 35), narration, description (2–4 Sätze, ohne URL), hashtags (3–6), evidence (Aussage + wörtliches Zitat), key_facts (1–3, Wert max. 12 und Label max. 28 Zeichen), date_label (max. 22).

### Titel, Beschreibung, Hashtags beim Upload (`upload.py: build_metadata`)

- **Titel:** aus dem Skript, ohne spitze Klammern, höchstens 100 Zeichen. Er darf nichts versprechen, was das Skript nicht einlöst (Prompt-Regel). Im Web lässt er sich bearbeiten.
- **Beschreibung:** Skript-Beschreibung + „Quelle: <URL>“ + Pixabay-Credit + „Alle Angaben ohne Gewähr, Stand bei Veröffentlichung.“ + Hashtags.
- **Hashtags:** aus dem Skript plus die Standard-Hashtags `#shorts #wasändertsichfürdich #verbraucher`, ohne Doppelte. Höchstens 15 davon gehen als Tags an YouTube.
- **Kategorie:** 25 (News & Politics), Sprache de, nicht für Kinder. `containsSyntheticMedia: false`.

### Stimme

- **Anbieter und Modell:** OpenAI `gpt-4o-mini-tts`, Stimme `nova`, Ausgabe als WAV.
- **Tempo:** ×1,25 per ffmpeg atempo (`TTS_SPEED`).
- **Tonfall-Anweisung** (`tts_instructions` in channels.yaml): „Sprich wie eine freundliche, sachliche Nachrichtensprecherin auf Deutsch. Zügiges Tempo, klare Betonung von Zahlen und Daten.“
- **Untertitel-Timing:** Whisper. Weicht die Wortzahl ab, wird geschätzt.

### Video

- **Länge:** etwa 24–28 s bei v2, 27–33 s bei v1. Am Ende kommen 0,4 s Nachlauf dazu.
- **Aufbau v2:**
  - Ab Frame 0 steht der erste Satz als große Tafel im Bild (Gelb auf Navy, Montserrat Black), bis der Satz endet, mindestens 1,6 s. Währenddessen gibt es keine Untertitel und keine Titelbox.
  - Danach kommen 1–3 Faktenkarten, gleichmäßig über die restliche Sprechzeit verteilt, mit Datums-Pill. Die letzte Karte bleibt bis zum letzten Frame, damit der Loop sauber ist.
  - Wort-Untertitel in Gruppen von höchstens 3 Wörtern, das aktive Wort gelb.
  - Kleine Titelbox oben.
- **Endkarte und Outro:** keine. Die Quelle steht nur in der Beschreibung.
- **Hintergrund:** Eigene Clips in `assets/backgrounds/` haben Vorrang, derzeit liegen dort keine. Danach Pixabay mit den Suchbegriffen fireworks, traffic timelapse, hyperlapse, ink, particles, abstract, roller coaster und drone city; Tag-Filter, abgedunkelt und weichgezeichnet. Als Rückfall dient eine generierte Ball-Animation.
- **Musik und Soundeffekte:** keine. Die Stimme wird auf −14 LUFS normalisiert.

### Veröffentlichung

- **Upload-Modus:** `UPLOAD_MODE=api`.
- **Termine:** nächster freier Termin aus der Terminliste, mindestens 2 h in der Zukunft. Ein Termin ist belegt, wenn ein nicht gelöschtes Video ihn hat. Gespeichert wird er erst nach erfolgreichem Upload.
- **Ohne Audit geprüft:** Videos mit `publishAt` und `--sofort` werden tatsächlich öffentlich.
- **Mindestabstand:** ein Video pro Termin, also höchstens eins pro Tag. Einen anderen Abstand gibt es nicht.
- **Kontingent:** Meldet Google `quotaExceeded` oder `uploadLimitExceeded`, endet der Batch, und es gilt eine Sperre bis Mitternacht PT. Bei Rate-Limits und 5xx gibt es 3 Versuche mit Backoff.

## 5. Stand der Punkte aus dem Testbericht vom 20.11.

| Punkt | Stand | Kommentar | Commit |
|---|---|---|---|
| Upload-Ergebnis ehrlich melden | erledigt | Ergebnis „k von N hochgeladen – Grund“, ok/warn/error statt ✅ bei 0 Uploads | 999cf0c |
| Kontingent auf quotaExceeded/uploadLimitExceeded, 1.600-Einheiten-Rechnung raus | erledigt | Google entscheidet, Sperre bis Reset, eigene Zählung nur zur Info | d436887 |
| Thema-Datum erzwingen, VZ ohne Datum | erledigt | Datum von der Artikelseite, ohne Datum oder über 21 Tage → verworfen | 999cf0c |
| Termin-Vergabe ohne Kollision | erledigt | Termin erst nach Erfolg gespeichert, gelöschte geben ihren Termin frei, stabile Sortierung | d436887 |
| YouTube-Status in die DB, Spalte Sichtbarkeit | erledigt | öffentlich/privat/gelöscht und Veröffentlichungszeit, täglich 12:00 | 999cf0c |
| Exit-Codes bei check-public | erledigt | Befund = warn mit Exit 0, nur technische Fehler ≠ 0 | 999cf0c |
| Freigabe-Karte: Quelldatum, Titel/Beschreibung editierbar, Warn-Badges | erledigt | dazu seit v2 der Hinweis „Mögliche Dublette“ | 0849be9 |
| Job erst fertig, wenn Video zur Freigabe bereit | erledigt (anders gelöst) | Fortschritt pro Schritt im Banner; Videos sind schon vor dem Telegram-Versand freigebbar | 0849be9 |
| Einstellungen: systemd-Timer weg, strukturierte Termine | teilweise | Termine strukturiert, Timer deaktiviert, aber die Units sind noch installiert und im Repo (`deploy/`) | 0849be9 |
| Mobil, 404-Seiten, CSRF, Security-Header, /media nur mit Login | teilweise | Mobil und 404/500 erledigt; /media nur mit Login, aber per 303-Redirect statt 401; **CSRF-Token und Security-Header (CSP, nosniff, Referrer-Policy) fehlen**, Meldungen noch über `?msg=` | 0849be9 |

## 6. Zahlen aus der DB

### Themen nach Status

| neu | skript | video | wartet | freigegeben | hochgeladen | abgelehnt | verworfen | fehler | davon gelöscht (yt) |
|---|---|---|---|---|---|---|---|---|---|
| 9 | 0 | 0 | 0 | 0 | 22 | 1 | 91 | 1 | 6 |

Insgesamt 124 Themen. Die IDs springen bis 2877, das ist harmlos: SQLite vergibt IDs auch bei ignorierten Dubletten-Inserts.

### Häufigste Verwerfungsgründe (14 Tage, 91 verworfen)

| Grund | Anzahl |
|---|---|
| Vorauswahl: kein Verbraucherbezug (Diplomatie, Institutionelles, Ausschreibungen, Haushalt) | 64 |
| Opus: keine konkrete Änderung, Datum fehlt, nur Bericht oder Strategie | 18 |
| Vorfilter (Blockliste) | 5 |
| Quelle zu alt (39, 81, 102 Tage) | 3 |
| Dublette (Vorauswahl) | 1 |

### Hochgeladene Videos

| # | Titel (gekürzt) | YouTube-ID | Termin bzw. veröffentlicht (UTC) | Sichtbarkeit | Länge s | Wörter | Stil |
|---|---|---|---|---|---|---|---|
| 1 | Frühstartrente: 10 Euro im Monat vom Staat fürs Kinder… | 7oM_nNX4E3k | 2026-11-20 12:42 | öffentlich | 46,0 | 94 | v1 |
| 105 | Beitragsbemessungsgrenzen 2027: Neue Grenzen für Gutve… | tE-6-GO0Ltg | 2026-11-20 16:00 | gelöscht | 42,0 | 83 | v1 |
| 619 | Digitale Brieftasche d-you: Ausweis aufs Handy ab Anfa… | eJAffJeOHUM | 2026-11-20 18:30 | öffentlich | 31,3 | 71 | v1 |
| 622 | Reform geplant: 116 117 soll bei akuten Fällen erstein… | ZcnGaQggbhY | 2026-11-20 18:30 | öffentlich | 27,9 | 74 | v1 |
| 3 | Stromsteuer soll 2027 sinken: von 2,05 auf 0,1 Cent pr… | W90qSHtocSE | 2026-11-20 20:00 | öffentlich | 29,0 | 73 | v1 |
| 4 | Stromsteuer soll 2027 sinken – auch für private Hausha… | 95G3BBEEHM4 | 2026-11-20 20:03 | öffentlich | 31,3 | 74 | v1 |
| 10 | Unterhaltsvorschuss: Einkommensgrenze für Alleinerzieh… | EMp8HtCQZwI | 2026-11-21 22:39 | öffentlich | 27,5 | 69 | v1 |
| 623 | Führerschein-Reform: Theorie per App, keine feste Zahl… | 20S56Pn2AfQ | 2026-11-22 10:00 | öffentlich | 29,3 | 75 | v1 |
| 95 | DAZN-Sammelklage gewonnen: Wer Geld zurückbekommen kann | sdtdNk6gu0s | 2026-11-22 17:17 | öffentlich | 29,4 | 74 | v1 |
| 102 | ChatGPT zeigt jetzt Werbung: Free und Go betroffen, so… | 6jpgMFPaFok | 2026-11-23 16:00 | gelöscht | 30,6 | 71 | v1 |
| 18 | Private Altersvorsorge: 50 Cent Staatszulage pro Spar-… | ETv62sd879Q | 2026-11-23 16:00 | öffentlich | 26,8 | 68 | v2-hook-frame0 |
| 8 | Befristete Jobs: Kabinett plant bis zu vier Jahre ohne… | Z2Im5VRNLUs | 2026-11-24 16:00 | öffentlich | 26,8 | 69 | v1 |
| 9 | Tankrabatt endet Ende 2026 – Spritpreisdeckel für 2027… | Wfzy5GXWndM | 2026-11-25 16:00 | privat | 27,1 | 74 | v1 |
| 64 | Kabinett-Entwurf: Befristung ohne Sachgrund soll erwei… | is5X6O9uRZ8 | 2026-11-26 16:00 | gelöscht | 32,7 | 75 | v1 |
| 96 | Neues Kreditrecht seit 20. November 2026: Das gilt für… | cLVHFQXZjBU | 2026-11-26 16:00 | privat | 24,7 | 68 | v2-hook-frame0 |
| 12 | Rentenbeitrag bleibt 2027 bei 18,6 Prozent – Rentenber… | d33V977RWp4 | 2026-11-27 16:00 | privat | 29,1 | 68 | v1 |
| 13 | Stromsteuer soll 2027 auf 0,1 Cent sinken: Das ist gep… | 0u644UmQb4Q | 2026-11-28 10:00 | gelöscht | 30,8 | 72 | v1 |
| 97 | Schufa & Co.: Diese Daten sind fürs Scoring jetzt verb… | 1BgHKEDIFGI | 2026-11-28 10:00 | privat | 28,0 | 66 | v2-hook-frame0 |
| 14 | Spritsteuer: etwa 17 Cent weniger pro Liter bis Ende 2… | s3Y2ZU5c-wY | 2026-11-29 10:00 | gelöscht | 28,5 | 72 | v1 |
| 99 | Digital Fairness Act: EU plant Verbot von Designtricks… | 7nVRDk1GWOg | 2026-11-29 10:00 | privat | 26,4 | 67 | v2-hook-frame0 |
| 16 | Strom: Netzentgelt-Zuschuss verlängert, Stromsteuer so… | Ox5Ui4f2bbE | 2026-11-30 16:00 | gelöscht | 32,1 | 73 | v1 |
| 2877 | Kfz-Versicherung kündigen: Bis 30. November muss sie d… | -7pCjLKPvS8 | 2026-11-30 16:00 | privat | 24,5 | 68 | v2-hook-frame0 |

### Geplant, noch nicht veröffentlicht (Stand 25.11. vormittags)

| Termin (Berlin) | # | Thema |
|---|---|---|
| Mi 25.11. 17:00 | 9 | Tankrabatt endet / Spritpreisdeckel |
| Do 26.11. 17:00 | 96 | Neues Kreditrecht |
| Fr 27.11. 17:00 | 12 | Rentenbeitrag bleibt 18,6 % |
| Sa 28.11. 11:00 | 97 | Schufa: verbotene Scoring-Daten |
| So 29.11. 11:00 | 99 | Digital Fairness Act |
| Mo 30.11. 17:00 | 2877 | Kfz-Versicherung kündigen, **Frist 30.11.** ⚠️ erscheint erst am Fristtag |

Der Vorrat reicht 6 Tage, bis einschließlich 30.11. Ab Di 01.12. ist nichts mehr geplant. Offen in `neu`: 9 Themen, davon 1 noch ohne Vorauswahl.

### Kosten (aus `cost_json`; Claude exakt aus Tokens, TTS und Whisper geschätzt)

| | USD | ≈ EUR (0,90) |
|---|---|---|
| Gesamt seit Start (20.11.) | 3,40 | 3,06 |
| Letzte 7 Tage | 3,40 (= gesamt) | 3,06 |
| Ø pro fertigem Video (20 gerendert; Kosten verworfener Themen eingerechnet) | 0,17 | 0,15 |
| davon Claude Opus | 3,16 (93 %) | |
| davon Vorauswahl Haiku | 0,006 | |
| davon TTS | 0,18 | |
| davon Whisper | 0,06 | |

## 7. Gesundheit

- **Tests:** 103 grün, 0 rot (pytest im Container, 25.11.). pyflakes ohne Befund.
- **Letzte 10 Jobs:**

| Job | Art | Auslöser | Start (UTC) | Ergebnis |
|---|---|---|---|---|
| 55 | check-public | Web | 25.11. 10:16 | warn: 22 geprüft (10 öffentlich, 6 privat, 6 gelöscht) |
| 54 | upload --only 2877 | Web | 24.11. 13:42 | ok: 1 von 1 hochgeladen |
| 53 | pipeline --limit 1 | Web | 24.11. 13:39 | ok: 1 Video, 4 verworfen (3× Vorauswahl, 1× Dublette) |
| 52 | check-public | Zeitplan | 24.11. 11:00 | warn: 21 geprüft (9 öffentlich) |
| 51 | check-public | Zeitplan | 23.11. 11:00 | warn: 21 geprüft (8 öffentlich) |
| 50 | upload --only 99 | Web | 22.11. 21:28 | ok: 1 von 1 |
| 49 | pipeline --limit 1 | Web | 22.11. 21:26 | ok: 1 Video, 0 verworfen |
| 48 | upload --only 97 | Web | 22.11. 21:25 | ok: 1 von 1 |
| 47 | upload --only 97 | Web | 22.11. 21:24 | warn: nichts freigegeben (zu früh geklickt) |
| 46 | pipeline --limit 1 | Web | 22.11. 21:23 | ok: 1 Video, 1 verworfen (zu alt) |

check-public meldet `warn`, weil die 6 gelöschten Videos als Befund zählen. Das ist erwartet, aber laut.

- **Fehler der letzten 7 Tage:**
  - Job-Logs: 4× „Skript fehlgeschlagen“. Thema #6 scheiterte 3× an „Erster Satz spricht nicht mit 'du' an“ (auch nach dem Korrekturversuch) und steht auf `fehler`. Thema #10 scheiterte einmal und lief danach durch.
  - Container-Logs: 5× `telegram.error.NetworkError: Bad Gateway` im Bot, dazu jeweils eine fehlgeschlagene Telegram-Benachrichtigung; 1× ConnectError beim Aktualisieren einer Nachricht. Vorübergehend, der Bot läuft weiter.
- **Bekannte Bugs und Workarounds:**
  - In `app_settings` steht noch die alte `upload_block` vom 21.11. (bis 22.11.). Sie ist abgelaufen und wirkungslos, wird aber nicht aufgeräumt.
  - Die Termin-Logik kennt keine Fristen. #2877 (Frist 30.11.) erscheint am 30.11. um 17:00. Workaround: das Video von Hand vorziehen oder sofort veröffentlichen.
  - Die Dubletten #3 und #4 (beide Stromsteuer) sind weiter öffentlich und haben praktisch keine Reichweite.
  - Weicht die Wortzahl bei Whisper ab, werden die Untertitel geschätzt und laufen ungenau.
  - `CLAUDE.md` ist stellenweise veraltet: dort stehen noch „70–110 Wörter“, das Kontingent in Einheiten und der Stand vom 20.11. Der Code ist maßgeblich.

## 8. Offene Baustellen und Empfehlung

### Angefangen, nicht fertig

- **Vergleich v1 gegen v2:** Die Stil-Version wird gespeichert, ausgewertet ist noch nichts. Seit 23.11. sind 5 v2-Videos hochgeladen, davon ist bisher eines öffentlich (#18).
- **P2 aus dem Testbericht:** CSRF, Security-Header, Meldungen über die Session statt `?msg=`, `/media` mit 401, Seiten „Videos“ und „Themen“, gespeicherter Ablehnungsgrund.
- **Alte Zeitsteuerung:** Die systemd-Units sind noch installiert, und es gibt zwei Mechanismen zur Zeitsteuerung.
- **YouTube-Audit:** offen, Uploads funktionieren aber auch ohne.

### Top 5 technische Schulden

1. **Analytics nur von Hand** über die KI in YouTube Studio. Es gibt keinen API-Import, und eine Rückkopplung zu Hook, Stil und Thema ist nicht automatisierbar.
2. **Sicherheit des Webinterface:** kein CSRF-Schutz, keine Security-Header. Es ist nur lokal erreichbar, also ein geringes Risiko, aber offen.
3. **Keine automatische Datensicherung** von `data/pipeline.sqlite3` und `credentials/`; es gibt nur eine manuelle Kopie vom 22.11.
4. **Zwei Zeitsteuerungen nebeneinander:** systemd-Units plus APScheduler. Hinzu kommen Migrationen ausschließlich mit TEXT-Spalten, z. B. `triage_score` als Text.
5. **Feste Preise** in `costs.py` und `.env` sowie geschätzte TTS- und Whisper-Kosten. Ändern sich die Preise, merkt das niemand.

### Empfehlung, was als Nächstes kommt

1. **Fristen bei der Terminvergabe beachten:** Ein Video mit Frist (`date_label` „BIS …“) muss mindestens 3 Tage vor der Frist erscheinen, sonst wird es sofort veröffentlicht oder es kommt eine Warnung. Anlass: Der Kfz-Fall zeigt, dass ein gutes Video sonst wertlos wird.
2. **Vorrat sichern:** den Web-Zeitplan „Erstellen“ einschalten, z. B. täglich 1 Video, plus Telegram-Warnung, wenn weniger als 3 Tage geplant sind. Sonst ist ab 01.12. Pause. Ob das automatisch laufen soll, entscheidet der Betreiber.
3. **YouTube Analytics API anbinden** (Scope `yt-analytics.readonly`): Aufrufe, Anteil Angesehen statt Weggewischt und Ø-Dauer täglich in die DB, im Dashboard v1 gegen v2. Das ist die Grundlage für alle weiteren Gestaltungsentscheidungen.
4. **Quellen ausbauen:** Verbraucherzentrale (5 von 15 Themen online) und Bundesregierung – Artikel (11 von 32) liefern. Pressemitteilungen der Bundesregierung (0 von 26), BMF (0 von 20) und BNetzA (0 von 11) liefern praktisch nichts. Besser wären weitere verbrauchernahe Quellen wie Stiftung Warentest, Finanztip-Nachrichten, Deutsche Rentenversicherung oder GKV, und dafür schwache Feeds herausnehmen. Das senkt auch die Kosten pro Video.
5. **P2-Sicherheit** (CSRF und Header) und eine **tägliche DB-Sicherung**: kleiner Aufwand, schließt die offensichtlichen Lücken.
