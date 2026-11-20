from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from shorts import backgrounds, sample_data
from shorts.backgrounds import (choose_pixabay_rendition, credit_for, generate_clip, pick_background,
                                pixabay_search)
from shorts.db import Database
from shorts.graphics import build_cards, render_fact_card
from shorts.render import probe_duration
from shorts.script import ShortScript, unverified_numbers

HIT = {
    "id": 125, "pageURL": "https://pixabay.com/videos/id-125/", "duration": 20, "tags": "sand",
    "videos": {
        "large": {"url": "https://cdn/l.mp4", "width": 1920, "height": 1080, "size": 9},
        "medium": {"url": "https://cdn/m.mp4", "width": 1280, "height": 720, "size": 5},
        "small": {"url": "https://cdn/s.mp4", "width": 960, "height": 540, "size": 2},
        "tiny": {"url": "", "width": 0, "height": 0, "size": 0},
    },
}


# ------------------------------------------------------------------ Pixabay
def test_rendition_genug_hoehe_fuer_hochkant() -> None:
    assert choose_pixabay_rendition(HIT)["url"] == "https://cdn/l.mp4"
    only_small = {"videos": {"small": HIT["videos"]["small"]}}
    assert choose_pixabay_rendition(only_small) is None  # 540 px hoch wäre auf 1920 zu unscharf


def test_suche_wird_24h_gecacht(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    class Resp:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict: return {"hits": [HIT]}

    def fake_get(url: str, **kw):
        calls.append(kw["params"])
        return Resp()

    monkeypatch.setattr(backgrounds.httpx, "get", fake_get)
    monkeypatch.setattr(backgrounds, "MIN_REQUEST_INTERVAL", 0)
    t0 = 1_000_000.0
    key = "GEHEIMER-TESTKEY-123"
    assert pixabay_search(key, "kinetic sand", tmp_path, now=t0)[0]["id"] == 125
    pixabay_search(key, "kinetic sand", tmp_path, now=t0 + 23 * 3600)   # aus dem Cache
    assert len(calls) == 1
    pixabay_search(key, "kinetic sand", tmp_path, now=t0 + 25 * 3600)   # abgelaufen → neu
    assert len(calls) == 2
    assert "orientation" not in calls[0] and calls[0]["safesearch"] == "true"
    cache_text = "".join(f.read_text() for f in tmp_path.glob("search_*.json"))
    assert key not in cache_text  # API-Key landet nie im Cache


def test_pixabay_clip_liefert_quellenangabe(tmp_path: Path) -> None:
    pool = tmp_path / "cache" / "pixabay"
    pool.mkdir(parents=True)
    (pool / "pixabay_125.mp4").write_bytes(b"x")
    (pool / "pixabay_125.json").write_text(json.dumps({"id": 125, "pageURL": HIT["pageURL"]}))
    backgrounds.POOL_TARGET, old = 1, backgrounds.POOL_TARGET  # Pool voll → keine Requests
    try:
        choice = pick_background(assets_dir=tmp_path / "assets", cache_dir=tmp_path / "cache", seed=0,
                                 pixabay_api_key="k", queries=["x"])
    finally:
        backgrounds.POOL_TARGET = old
    assert choice.path.name == "pixabay_125.mp4"
    assert choice.credit == "Video: Pixabay https://pixabay.com/videos/id-125/" == credit_for(HIT)


def test_eigene_clips_haben_vorrang(tmp_path: Path) -> None:
    (tmp_path / "assets" / "backgrounds").mkdir(parents=True)
    clip = tmp_path / "assets" / "backgrounds" / "eigenes_gameplay.mp4"
    clip.write_bytes(b"x")
    choice = pick_background(assets_dir=tmp_path / "assets", cache_dir=tmp_path / "cache", seed=3,
                             pixabay_api_key="unbenutzt", queries=["x"])
    assert choice.path == clip and choice.credit is None


def test_db_migration_ergaenzt_spalte(tmp_path: Path) -> None:
    import sqlite3
    path = tmp_path / "alt.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, channel TEXT, url TEXT, source_name TEXT,"
                " title TEXT, summary TEXT, published_at TEXT, source_text TEXT, status TEXT, prev_status TEXT,"
                " script_json TEXT, audio_path TEXT, video_path TEXT, note TEXT, telegram_message_id INTEGER,"
                " publish_at TEXT, youtube_id TEXT, created_at TEXT, updated_at TEXT, UNIQUE(channel,url))")
    con.close()
    db = Database(path)
    tid = db.add_topic("de", "https://a.de", "Q", "T")
    db.update_fields(tid, background_credit="Video: Pixabay x")
    assert db.get(tid).background_credit == "Video: Pixabay x"


# ------------------------------------------------------------------ Animation
def test_generierte_animation_ist_gueltiges_video(tmp_path: Path) -> None:
    out = generate_clip(tmp_path / "a.mp4", seconds=1.0, fps=10, size=(108, 192), seed=1)
    assert out.exists() and not (tmp_path / "a.part.mp4").exists()
    assert 0.8 < probe_duration(out) < 1.3


# ------------------------------------------------------------------ Faktenkarten
def test_faktenkarte_im_kanaldesign(tmp_path: Path) -> None:
    card = render_fact_card("30 Tage", "Rücksendefrist", "AB 1. MÄRZ", tmp_path / "c.png")
    img = Image.open(card)
    assert img.mode == "RGBA" and img.width == 920
    assert img.getpixel((5, img.height // 2))[:3] == (11, 31, 58)       # Navy-Fläche
    assert img.getpixel((img.width // 2, 5))[:3] == (255, 210, 63)      # gelbe Akzentleiste
    assert img.getpixel((0, 0))[3] == 0                                  # abgerundete, transparente Ecke


def test_sehr_lange_zahl_wird_verkleinert(tmp_path: Path) -> None:
    img = Image.open(render_fact_card("1.234.567 €", "Beispiel", "", tmp_path / "c.png"))
    assert img.width == 920


def test_karten_verteilen_sich_ueber_sprechzeit(tmp_path: Path) -> None:
    cards = build_cards(sample_data.SAMPLE_SCRIPT["key_facts"], "AB 1. MÄRZ", tmp_path, 1.0, 31.0)
    assert [(c.start, c.end) for c in cards] == [(1.0, 11.0), (11.0, 21.0), (21.0, 31.0)]
    assert build_cards([], "", tmp_path, 0, 10) == []
    only_date = build_cards([], "BIS ENDE 2027", tmp_path, 0, 10)
    assert len(only_date) == 1


def test_zahlenpruefung_der_grafik() -> None:
    s = ShortScript.model_validate(sample_data.SAMPLE_SCRIPT)
    assert unverified_numbers(s, sample_data.SAMPLE_SOURCE_TEXT) == []
    bad = s.model_copy(update={"date_label": "AB 1. APRIL 2028"})
    assert unverified_numbers(bad, sample_data.SAMPLE_SOURCE_TEXT) == ["AB 1. APRIL 2028"]


def test_pool_waechst_abwechslungsreich_und_gedrosselt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def hits_for(q: str) -> list[dict]:
        return [{**HIT, "id": f"{q}{i}", "tags": f"{q}, x", "pageURL": f"https://p/{q}{i}"} for i in range(5)]

    monkeypatch.setattr(backgrounds, "pixabay_search", lambda key, q, d: hits_for(q))
    monkeypatch.setattr(backgrounds, "MIN_REQUEST_INTERVAL", 0)

    class Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self) -> None: ...
        def iter_bytes(self): yield b"video"

    monkeypatch.setattr(backgrounds.httpx, "stream", lambda *a, **k: Stream())
    pool = backgrounds.fill_pixabay_pool("k", ["fireworks", "ink", "abstract", "hyperlapse"], tmp_path)
    assert len(pool) == 3                                   # max. 3 neue Clips pro Lauf
    assert {p.name.split("_")[1][:-5] for p in pool} == {"fireworks", "ink", "abstract"}  # je Begriff einer
    pool = backgrounds.fill_pixabay_pool("k", ["fireworks", "ink", "abstract", "hyperlapse"], tmp_path)
    assert any("hyperlapse" in p.name for p in pool)        # nächster Lauf startet beim nächsten Begriff


def test_tagfilter_verwirft_lockere_treffer() -> None:
    from shorts.backgrounds import matches_query
    assert not matches_query({"tags": "ocean, waves, rocks, sand, coast"}, "kinetic sand")  # Strand
    assert matches_query({"tags": "kinetic sand, sand, cutting, asmr"}, "kinetic sand")
    assert matches_query({"tags": "light traces, traffic, highway, time lapse, timelapse"}, "traffic timelapse")


def test_datum_wird_nicht_doppelt_gezeigt(tmp_path: Path) -> None:
    from shorts.graphics import _same_info
    assert _same_info("Ende 2027", "BIS ENDE 2027")
    assert _same_info("1. März", "AB 1. MÄRZ")
    assert not _same_info("30 Tage", "AB 1. MÄRZ")
    with_pill = Image.open(build_cards([{"value": "30 Tage", "label": "x"}], "AB 1. MÄRZ", tmp_path, 0, 5)[0].path)
    no_pill = Image.open(build_cards([{"value": "Ende 2027", "label": "x"}], "BIS ENDE 2027", tmp_path, 0, 5)[0].path)
    assert no_pill.height < with_pill.height  # Karte ohne Datums-Feld ist niedriger
