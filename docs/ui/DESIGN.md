# Designsystem Webinterface

Stand 25.11.2026. Entstanden mit dem Skill [unslop](https://github.com/heydamir/unslop) von Damir Sokolovsky
(@heydamir, CC BY 4.0). Gilt für alle Seiten unter `src/shorts/web/templates/`. Neue Seiten und Änderungen halten
sich daran; die CSS-Variablen stehen in `base.html`, die Bausteine in `_macros.html`.

## Richtung

- **Produkt:** Steuerpult für einen Ein-Personen-Kanal, der neue Gesetze, Preise und Fristen als Shorts
  veröffentlicht – freigeben, einplanen, auswerten.
- **Charakter:** nüchtern, verlässlich, redaktionell.
- **Welt:** Sendeplan einer Nachrichtenredaktion – Papier-Terminkalender, Textmarker. Marke aus den Videos:
  Navy `#0B1F3A`, Gelb `#FFD23F`.
- **Momente:** die Zeitleiste „Nächste 7 Termine“ auf Heute und das Diagramm auf Statistik. Sonst ist alles ruhig.
- **Signatur:** ein gelber Textmarker-Strich (wie die Akzentleiste auf den Videotafeln). Er markiert den aktiven
  Menüpunkt, den nächsten Termin und laufende Jobs – und sonst nichts.

## Seiten (je eine Aufgabe)

| Seite | Pfad | Aufgabe |
|---|---|---|
| Heute | `/` | 4 Kennzahlen, „Braucht dich“ (nur wenn nicht leer), nächste 7 Termine, Erstellen. Passt bei leerer Box auf 1440×900 |
| Freigabe | `/freigabe` | Freigabe-Karten (Video, Tafel, Skript, Belege, Warnungen, Kosten) und „Freigegeben – bereit zum Hochladen“ |
| Videos | `/videos?tab=…`, `/videos/<id>` | Reiter Geplant · Öffentlich · Abgelehnt/Verworfen · Gelöscht; Zeile klickbar → Detailseite mit allem |
| Statistik | `/statistik` | Diagramm angesehen vs. weggewischt, Stil-Vergleich, ein Sammelformular für Studio-Werte |
| Recherche | `/recherche`, `/recherche/<id>` | Lauf, Sichtungsstand, Treffer; Aussortierte eingeklappt; Sichtung auf der Detailseite |
| Jobs | `/jobs`, `/jobs/<id>` | Protokoll mit Filter (Alle/Fehler/Warnungen) und Seiten |
| Einstellungen | `/einstellungen` | Zeitplan, Termine, Passwort |

Alte Adressen (`/settings`, `/research`, `/research/<id>/review`, `/#t<id>`) funktionieren weiter.
Navigation: Freigabe zeigt die Zahl wartender Videos, Jobs „läuft“ oder die Zahl der Fehler der letzten 24 h.

## Tokens

| Rolle | Wert |
|---|---|
| Seite / Karte / angehoben | `#f5f7fa` / `#ffffff` / `#eceff4` |
| Linien | Text-Farbe mit 8 % (`rgba(17,24,39,.08)`), kräftiger 14 % |
| Text primär / sekundär / gedämpft | `#111827` / `#4b5565` / `#6b7487` |
| Primärknopf | Navy `#0b1f3a`, weiße Schrift; sekundär: Karte mit Linie |
| Akzent | Gelb `#ffd23f` nur als Textmarker (Navigation, nächster Termin, laufender Job) |
| Semantik | ok `#15803d`/`#ecf8f0`, Hinweis `#92400e`/`#fdf5e6`, Fehler `#b91c1c`/`#fdf0f0`, Info Navy/`#eaeff6` |
| Stilfarben (nur Diagramm) | v1 `#94a3b8`, v2 `#0b1f3a`, v3 `#d4a017` |

- **Abstände:** Raster 4 px. Karten 20 px Innenabstand, Kennzahlen 16/20, Tabellenzellen 12/16, Abstand zwischen
  Abschnitten 32 px, Seitenrand 32 px (mobil 16 px).
- **Radien:** Badge/Tag 6, Knopf/Feld 8, Karte 12.
- **Schrift:** Systemschrift, Größen 12/13/14/15/16/24/28, Gewichte 400 und 500. Keine Versalien-Labels, keine
  Fettung über 600, Zahlen mit `tabular-nums`, Zahlenspalten rechtsbündig, Datumsspalten linksbündig.
- **Icons:** Lucide (ISC) als lokale Schrift `/static/lucide.ttf`, 16 px, Farbe gedämpft. Keine Emojis in der Oberfläche.
- **Bewegung:** nur Farb-/Rahmenwechsel 150 ms, Druck `scale(.98)`, eine Kurve `cubic-bezier(.22,1,.36,1)`.

## Bausteine (`_macros.html`)

- `badge(state)` – **einzige** Statusanzeige. Farben über `TONE`: öffentlich/freigegeben/in Kraft/beschlossen = ok,
  wartet/Entwurf/Frist = Hinweis, Fehler/gelöscht/abgelehnt = Fehler, geplant/in Arbeit/läuft = Info, frei/verworfen = neutral.
- `law(status)` – Rechtsstatus einer Merkliste (geplant hier neutral).
- `job_badge(job)`, `style("v2")` (Stil-Tag in Mono), `when(iso)` (Datum + relative Angabe), `icon(code)`.

## Texte

- Ein Datumsformat überall: **„Mo 23.11., 17:00“** (`| dt`), Tag ohne Uhrzeit `| day`; relative Angaben nur als
  Zusatz in Grau (`| rel`: „heute“, „morgen“, „in 2 Tagen“).
- Fehler in Klartext (`views.humanize_error`), technisches Detail aufklappbar.
- Keine Ausrufezeichen, keine Punkte am Ende von Labels, Knöpfen und Meldungen, keine Begrüßung.
- Gelöschte Videos zeigen nie einen Termin, sondern „gelöscht (erkannt …)“.

## Mobil (390 px)

Keine horizontale Scrollleiste. Tabellen werden zu Karten; `table.compact` blendet Feldnamen aus, `td.inline` stellt
kurze Werte (Stil, Status, Zahlen) nebeneinander.

## Prüfung

`./deploy/ui_check.sh` startet das Webinterface auf einer Kopie der Datenbank (eigenes Testpasswort, kein Zeitplan)
und lässt den offiziellen Playwright-Container (`mcr.microsoft.com/playwright/python:v1.49.1-noble`) alle Seiten bei
1440×900 und 390×844 fotografieren (`docs/ui/*.png`). Geprüft werden: Heute ≤ 900 px bei leerer „Braucht dich“-Box,
keine horizontale Scrollleiste bei 390 px, keine Konsolenfehler.
