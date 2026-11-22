from __future__ import annotations

import pytest

from shorts import sample_data
from shorts.db import Database, Status, TransitionError
from shorts.script import ShortScript, unverified_quotes, validate_script, word_count
from shorts.sources import normalize_url, parse_html_links, parse_rss
from shorts.subtitles import ass_time, build_ass, chunk_words, estimate_timings, split_words


# ---------------------------------------------------------------- db / Status-Maschine
@pytest.fixture
def db() -> Database:
    return Database(":memory:")


def test_add_topic_dedupliziert_pro_kanal(db: Database) -> None:
    assert db.add_topic("de", "https://a.de/x", "Q", "T") is not None
    assert db.add_topic("de", "https://a.de/x", "Q", "T") is None
    assert db.add_topic("en", "https://a.de/x", "Q", "T") is not None  # anderer Kanal ok


def test_gueltiger_durchlauf(db: Database) -> None:
    tid = db.add_topic("de", "https://a.de/x", "Q", "T")
    for st in (Status.SKRIPT, Status.VIDEO, Status.WARTET, Status.FREIGEGEBEN, Status.HOCHGELADEN):
        assert db.transition(tid, st).status == st
    assert db.get(tid).prev_status == Status.FREIGEGEBEN


def test_unerlaubter_uebergang(db: Database) -> None:
    tid = db.add_topic("de", "https://a.de/x", "Q", "T")
    with pytest.raises(TransitionError):
        db.transition(tid, Status.FREIGEGEBEN)  # Freigabe ohne Video/Review unmöglich
    assert db.get(tid).status == Status.NEU


def test_upload_zaehlung(db: Database) -> None:
    db.count_upload("2026-11-20")
    db.count_upload("2026-11-20")
    assert db.uploads_on("2026-11-20") == 2
    assert db.uploads_on("2026-11-21") == 0


# ---------------------------------------------------------------- sources
def test_normalize_url_entfernt_tracking() -> None:
    assert normalize_url("https://WWW.Bund.de/a?utm_source=x&id=3#top") == "https://www.bund.de/a?id=3"
    assert normalize_url("https://x.de/f.xml?nn=693926") == "https://x.de/f.xml"


def test_parse_rss() -> None:
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
      <item><title>Neue Regel &amp; mehr</title><link>https://bund.de/a?utm_x=1</link>
      <description>&lt;p&gt;Kurz&lt;/p&gt;</description><pubDate>Mon, 16 Nov 2026 10:00:00 +0100</pubDate></item>
      <item><title>Ohne Link</title></item></channel></rss>"""
    items = parse_rss(rss)
    assert len(items) == 1
    assert items[0].title == "Neue Regel & mehr"
    assert items[0].url == "https://bund.de/a"
    assert items[0].summary == "Kurz"
    assert items[0].published_at.startswith("2026-11-16T09:00")


def test_parse_html_links_filtert_und_dedupliziert() -> None:
    html = """<a href="/wissen/geld/neue-frist-123">Neue <b>Frist</b></a>
              <a href="/wissen/geld/neue-frist-123"></a>
              <a href="/impressum-119462">Impressum</a>"""
    items = parse_html_links(html, "https://vz.de/", r"^/wissen/[a-z0-9/_-]+-\d+$", 10)
    assert [(i.url, i.title) for i in items] == [("https://vz.de/wissen/geld/neue-frist-123", "Neue Frist")]


# ---------------------------------------------------------------- script-Validierung
def _script(**over) -> ShortScript:
    return ShortScript.model_validate({**sample_data.SAMPLE_SCRIPT, **over})


def test_beispielskript_ist_gueltig() -> None:
    s = _script()
    from shorts.script import MAX_WORDS, MIN_WORDS
    assert MIN_WORDS <= word_count(s.narration) <= MAX_WORDS
    assert validate_script(s) == []


def test_zu_kurzes_skript_wird_bemaengelt() -> None:
    assert any("Wörter" in p for p in validate_script(_script(narration="Viel zu kurz.")))


def test_verworfenes_skript_braucht_keine_pruefung() -> None:
    assert validate_script(_script(discard=True, narration="", evidence=[])) == []


def test_zitatpruefung_findet_erfundene_belege() -> None:
    s = _script(evidence=[
        {"claim": "a", "quote": "Bisher lag die Frist bei 14 Tagen."},           # steht drin
        {"claim": "b", "quote": "Ab dem 1. März gilt im Beispielland eine verlängerte"},  # über Zeilenumbruch
        {"claim": "c", "quote": "Die Frist beträgt 60 Tage."},                   # erfunden
    ])
    assert unverified_quotes(s, sample_data.SAMPLE_SOURCE_TEXT) == ["Die Frist beträgt 60 Tage."]


# ---------------------------------------------------------------- Untertitel
def test_timings_monoton_und_im_fenster() -> None:
    words = split_words(sample_data.SAMPLE_SCRIPT["narration"])
    t = estimate_timings(words, 0.2, 30.0)
    assert len(t) == len(words)
    assert t[0].start == pytest.approx(0.2)
    assert t[-1].end == pytest.approx(30.0, abs=0.01)
    assert all(a.end <= b.start + 1e-6 and a.start < a.end for a, b in zip(t, t[1:]))


def test_satzende_bekommt_pause() -> None:
    t = estimate_timings(["Haus.", "Haus"], 0, 10)
    assert (t[0].end - t[0].start) > (t[1].end - t[1].start)


def test_chunks_kurz_und_vollstaendig() -> None:
    words = split_words("Ab dem ersten März gilt eine neue Regel. Bisher waren es vierzehn Tage.")
    chunks = chunk_words(estimate_timings(words, 0, 10))
    assert sum(len(c) for c in chunks) == len(words)
    assert all(len(c) <= 3 for c in chunks)
    ends = [c[-1].word for c in chunks]
    i = ends.index("Regel.")  # Satzende beendet die Gruppe ...
    assert chunks[i + 1][0].word == "Bisher"  # ... und der nächste Satz beginnt neu


def test_ass_time_format() -> None:
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(61.257) == "0:01:01.26"
    assert ass_time(3725.5) == "1:02:05.50"


def test_build_ass_enthaelt_titel_quelle_und_escaping() -> None:
    t = estimate_timings(["Hallo", "{Welt}"], 0, 2)
    ass = build_ass(timings=t, title="Neu", source_label="bund.de", outro="", total_duration=5)
    assert "NEU" in ass and "Quelle: bund.de" in ass
    assert "(WELT)" in ass  # geschweifte Klammern dürfen keine ASS-Tags erzeugen


def test_hook_regeln() -> None:
    from shorts.script import hook_problems
    assert hook_problems("Dein Rückgaberecht wird mehr als doppelt so lang. Rest.") == []
    assert any("Wörter" in p for p in hook_problems(
        "Du bekommst ab sofort deutlich mehr Zeit für jede einzelne Rücksendung. Rest."))
    assert any("du" in p for p in hook_problems("Die Rente steigt um vier Prozent. Rest."))
    for opening in ("Ab dem ersten Januar zahlst du mehr.", "2027 zahlst du mehr Beitrag.",
                    "Zum 1. März sparst du Geld.", "Januar bringt dir mehr Geld."):
        assert any("Datums" in p for p in hook_problems(opening + " Rest.")), opening
    assert hook_problems("Abgeltungssteuer: Du sparst bald Geld. Rest.") == []  # 'Ab...' im Wort ist ok
