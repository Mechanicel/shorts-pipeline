from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shorts.db import Database
from shorts.research import (QuotaExceeded, ResearchConfig, ResearchResult, ResearchStore, YouTubeAPI, age_days,
                             channel_median, is_blocked_channel, is_short, outlier_score, parse_duration,
                             pattern_stats, render_report, run_research, telegram_summary)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
CFG = ResearchConfig(search_terms=["Rente", "Steuer"], own_channel_titles=["Was ändert sich für dich?"],
                     blocked_channels=["tagesschau", "Bild", "ARD"])


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- reine Kennzahlen
def test_dauer_und_short_erkennung() -> None:
    assert parse_duration("PT45S") == 45
    assert parse_duration("PT1M5S") == 65
    assert parse_duration("PT3M") == 180 and is_short(180)
    assert parse_duration("PT3M1S") == 181 and not is_short(181)
    assert parse_duration("PT1H2M") == 3720
    assert parse_duration("P0D") == 0 and not is_short(0)  # Livestream/Premiere ohne Dauer
    assert parse_duration("kaputt") is None and not is_short(None)


def test_median_score_und_alter() -> None:
    assert channel_median([100, 300, 200]) == 200
    assert channel_median([100, 200, 300, 400]) == 250
    assert channel_median([]) is None
    assert outlier_score(50_000, 2_000) == 25
    assert outlier_score(50_000, None) is None and outlier_score(50_000, 0) is None
    assert age_days(iso(2), NOW) == pytest.approx(2)
    assert age_days(iso(0), NOW) == pytest.approx(1 / 24)  # frisch: mind. 1 Stunde


def test_kanal_blockliste_mit_wortgrenzen() -> None:
    assert is_blocked_channel("Was ändert sich für dich?", "x", CFG) == "eigener Kanal"
    assert is_blocked_channel("tagesschau", "x", CFG).startswith("Medienmarke")
    assert is_blocked_channel("BILD", "x", CFG).startswith("Medienmarke")
    assert is_blocked_channel("Bildung mit Bernhard", "x", CFG) is None  # kein Teilwort-Treffer
    assert is_blocked_channel("Finanzen kompakt", "own", replace(CFG, own_channel_ids=["own"])) == "eigener Kanal"


def test_nur_deutsche_videos() -> None:
    from shorts.research import candidate_from_video, is_german
    ru = candidate_from_video(video("r", "c", 1, title="В самую богатую страну Европы за 0€?! 😱"), "t")
    de = candidate_from_video(video("d", "c", 1, title="195 € für die Schule. Reicht Kindergeld?"), "t")
    assert not is_german(ru) and is_german(de)
    en = video("e", "c", 1, title="Rente 2027")
    en["snippet"]["defaultAudioLanguage"] = "en-US"
    assert not is_german(candidate_from_video(en, "t"))
    en["snippet"]["defaultAudioLanguage"] = "de-DE"
    assert is_german(candidate_from_video(en, "t"))


def test_titelmuster_gezählt() -> None:
    vids = [{"title": "Achtung: 500 € zurück ab Januar?", "duration_s": 30, "channel_title": "A"},
            {"title": "Das musst du wissen", "duration_s": 55, "channel_title": "A"},
            {"title": "Rente 2027", "duration_s": 120, "channel_title": "B"}]
    s = pattern_stats(vids)
    tp = s["title_patterns"]
    assert tp["Zahl im Titel"]["count"] == 2 and tp["Frage"]["count"] == 1
    assert tp["du-Ansprache"]["count"] == 1 and tp["Betrag (€/Euro)"]["count"] == 1
    assert tp["Warnung (Achtung/Vorsicht/Warnung)"]["count"] == 1 and tp["Datum/Zeitpunkt"]["count"] == 2
    assert s["lengths"]["16–30 s"] == 1 and s["lengths"]["91–180 s"] == 1 and s["median_length"] == 55
    assert s["recurring_channels"] == [("A", 2)]


# ---------------------------------------------------------------- gemockte API
def video(vid, ch, views, seconds=40, days=3, title=None):
    return {"id": vid, "snippet": {"channelId": ch, "channelTitle": f"Kanal {ch}", "title": title or f"Video {vid}",
                                   "description": "d", "tags": ["rente"], "publishedAt": iso(days)},
            "contentDetails": {"duration": f"PT{seconds}S"},
            "statistics": {"viewCount": str(views), "likeCount": "10", "commentCount": "2"}}


class FakeYouTube:
    """Simuliert die REST-Antworten; zählt Aufrufe je Endpunkt."""

    def __init__(self, quota_after_searches: int | None = None):
        self.calls: list[str] = []
        self.quota_after = quota_after_searches
        self.search_results = {"Rente": ["v1", "v2", "v3", "v4"], "Steuer": ["v5", "v1"]}
        self.videos = {
            "v1": video("v1", "small", 120_000, title="Achtung: Rente steigt um 50 €"),  # Ausreißer
            "v2": video("v2", "big", 900_000),                                           # Kanal zu groß
            "v3": video("v3", "small", 5_000),                                           # zu wenig Aufrufe
            "v4": video("v4", "small", 80_000, seconds=400),                             # kein Short
            "v5": video("v5", "hidden", 60_000),                                         # versteckte Abos
            # Uploads für die Kanal-Mediane
            **{f"s{i}": video(f"s{i}", "small", 2_000 + i) for i in range(5)},
            **{f"h{i}": video(f"h{i}", "hidden", 4_000) for i in range(4)},
        }
        self.channels = {
            "small": {"id": "small", "snippet": {"title": "Kanal small"},
                      "statistics": {"subscriberCount": "8000", "hiddenSubscriberCount": False},
                      "contentDetails": {"relatedPlaylists": {"uploads": "UUsmall"}}},
            "big": {"id": "big", "snippet": {"title": "Kanal big"},
                    "statistics": {"subscriberCount": "500000", "hiddenSubscriberCount": False},
                    "contentDetails": {"relatedPlaylists": {"uploads": "UUbig"}}},
            "hidden": {"id": "hidden", "snippet": {"title": "Kanal hidden"},
                       "statistics": {"hiddenSubscriberCount": True},
                       "contentDetails": {"relatedPlaylists": {"uploads": "UUhidden"}}},
        }
        self.playlists = {"UUsmall": [f"s{i}" for i in range(5)], "UUhidden": [f"h{i}" for i in range(4)]}

    def __call__(self, endpoint, params):
        self.calls.append(endpoint)
        if endpoint == "search":
            if self.quota_after is not None and self.calls.count("search") > self.quota_after:
                raise QuotaExceeded("quotaExceeded")
            assert params["videoDuration"] == "short" and params["regionCode"] == "DE"
            assert params["order"] == "viewCount" and params["maxResults"] == 50
            return {"items": [{"id": {"videoId": v}} for v in self.search_results[params["q"]]]}
        if endpoint == "videos":
            ids = params["id"].split(",")
            assert len(ids) <= 50
            return {"items": [self.videos[i] for i in ids if i in self.videos]}
        if endpoint == "channels":
            return {"items": [self.channels[i] for i in params["id"].split(",")]}
        if endpoint == "playlistItems":
            return {"items": [{"contentDetails": {"videoId": v}} for v in self.playlists[params["playlistId"]]]}
        raise AssertionError(endpoint)


def test_lauf_findet_ausreisser_und_speichert_snapshot() -> None:
    fake = FakeYouTube()
    store = ResearchStore(Database(":memory:"))
    result = run_research(YouTubeAPI("k", get=fake), store, CFG, now=NOW)

    ids = [c.video_id for c in result.outliers]
    assert ids == ["v1", "v5"]                       # v1: 120k / Median 2002 ≈ 60×, v5: 60k / 4000 = 15×
    v1, v5 = result.outliers
    assert v1.score == pytest.approx(120_000 / 2002) and v1.subs == 8000
    assert v5.subs_hidden and v5.subs is None         # versteckte Abozahl → aufgenommen, markiert
    assert result.skipped == {"zu wenig Aufrufe": 1, "kein Short": 1, "Kanal zu groß": 1}
    assert result.search_calls == 2 and fake.calls.count("search") == 2
    assert "UUbig" not in str(fake.calls) and fake.calls.count("playlistItems") == 2  # großer Kanal ohne Median
    rows = store.outliers()
    assert [r["video_id"] for r in rows] == ["v1", "v5"] and rows[0]["n_snapshots"] == 1

    # zweiter Lauf: Kanal-Median aus dem Cache, neuer Snapshot fürs Wachstum
    fake2 = FakeYouTube()
    fake2.videos["v1"]["statistics"]["viewCount"] = "150000"
    run_research(YouTubeAPI("k", get=fake2), store, CFG, now=NOW)
    assert "channels" not in fake2.calls and "playlistItems" not in fake2.calls
    row = next(r for r in store.outliers() if r["video_id"] == "v1")
    assert row["views"] == 150_000 and row["first_views"] == 120_000 and row["n_snapshots"] == 2


def test_filter_score_und_suchlimit() -> None:
    fake = FakeYouTube()
    result = run_research(YouTubeAPI("k", get=fake), ResearchStore(Database(":memory:")),
                          replace(CFG, max_search_calls=1), now=NOW)
    assert fake.calls.count("search") == 1 and result.terms_searched == 1  # max_search_calls greift
    result = run_research(YouTubeAPI("k", get=FakeYouTube()), ResearchStore(Database(":memory:")),
                          replace(CFG, min_outlier_score=20), now=NOW)
    assert [c.video_id for c in result.outliers] == ["v1"]  # v5 nur 15× → unter 20
    assert result.skipped["kein Ausreißer"] == 1
    result = run_research(YouTubeAPI("k", get=FakeYouTube()), ResearchStore(Database(":memory:")),
                          replace(CFG, min_channel_videos=5), now=NOW)
    assert [c.video_id for c in result.outliers] == ["v1"]  # Kanal hidden hat nur 4 Uploads
    assert result.skipped["kein belastbarer Kanal-Median"] == 1
    result = run_research(YouTubeAPI("k", get=FakeYouTube()), ResearchStore(Database(":memory:")),
                          replace(CFG, min_channel_median=3000), now=NOW)
    assert [c.video_id for c in result.outliers] == ["v5"]  # Kanal small: Median 2002 < 3000
    assert result.skipped["Kanal-Median zu klein"] == 1


def test_themen_vorauswahl_und_mindest_median_im_bestand() -> None:
    from shorts.research import classify_outliers

    store = ResearchStore(Database(":memory:"))
    run_research(YouTubeAPI("k", get=FakeYouTube()), store, CFG, now=NOW)

    class FakeClassifier:
        spent = [0.0001]

        def classify(self, videos):
            assert {v["video_id"] for v in videos} == {"v1", "v5"}
            return {"v5": (False, "Satire")}  # v1 fehlt → im Zweifel behalten

    assert classify_outliers(store, FakeClassifier()) == (1, 1)
    rows = {r["video_id"]: r for r in store.outliers()}
    assert rows["v1"]["relevant"] == "ja" and rows["v5"]["relevant"] == "nein"
    assert rows["v5"]["relevant_reason"] == "Satire"
    assert classify_outliers(store, FakeClassifier()) == (0, 0)  # schon eingeordnet → kein neuer Aufruf
    assert store.drop_small_median(3000) == 1 and [r["video_id"] for r in store.outliers()] == ["v5"]


def test_kontingent_abbruch_mit_klarer_meldung() -> None:
    fake = FakeYouTube(quota_after_searches=1)
    store = ResearchStore(Database(":memory:"))
    result = run_research(YouTubeAPI("k", get=fake), store, CFG, now=NOW)
    assert result.quota_stop == "quotaExceeded"
    assert result.terms_searched == 1 and fake.calls.count("search") == 2  # 2. Aufruf scheitert
    assert "videos" not in fake.calls                  # nach dem Abbruch keine weiteren Aufrufe
    text = telegram_summary(result, [], None)
    assert "Abgebrochen: YouTube meldet quotaExceeded" in text


def test_http_kontingentfehler_wird_erkannt(monkeypatch) -> None:
    import httpx

    class Resp:
        status_code = 403

        def json(self):
            return {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp())
    with pytest.raises(QuotaExceeded):
        YouTubeAPI("geheim").search("Rente", iso(30))


def test_bericht_und_sichtung() -> None:
    store = ResearchStore(Database(":memory:"))
    result = run_research(YouTubeAPI("k", get=FakeYouTube()), store, CFG, now=NOW)
    top = store.outliers()
    md = render_report(result, top, pattern_stats(top), None, 0.0, 0.9, "2026-11-25",
                       excluded=[{"title": "Kabarett-Clip", "relevant_reason": "Satire"}])
    assert "| 1 | [Achtung: Rente steigt um 50 €](https://www.youtube.com/shorts/v1)" in md
    assert "versteckt" in md and "(< 5 Videos)" in md
    assert "1 Ausreißer als Politik, Satire oder Unterhaltung aussortiert" in md and "Kabarett-Clip" in md
    store.save_review("v1", {"face": "nein", "voice": "KI", "first_frame": "Text-Tafel", "hook_type": "Warnung",
                             "music": "unsinn", "notes": "  große Zahl  "})
    r = store.reviewed()[0]
    assert (r["voice"], r["first_frame"], r["music"], r["notes"]) == ("KI", "Text-Tafel", None, "große Zahl")
    assert store.review_count() == 1
    assert isinstance(result, ResearchResult)
