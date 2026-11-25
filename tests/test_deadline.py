from __future__ import annotations

from datetime import date, datetime, timezone

from shorts.deadline import (choose_slot, deadline_of, parse_deadline, planned_stock, stock_warning)

REF = date(2026, 11, 25)
NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)


def test_fristen_erkennen() -> None:
    d = parse_deadline("BIS 30. NOVEMBER 2026", REF)
    assert d.day == date(2026, 11, 30) and d.kind == "Frist" and d.latest_publish_day == date(2026, 11, 27)
    assert parse_deadline("AB 1. JANUAR 2027", REF).day == date(2027, 1, 1)
    assert parse_deadline("AB 1. JANUAR 2027", REF).kind == "Stichtag"
    assert parse_deadline("BIS 15. JANUAR", REF).day == date(2027, 1, 15)   # ohne Jahr: nächstes Vorkommen
    assert parse_deadline("BIS ENDE 2026", REF).day == date(2026, 12, 31)
    assert parse_deadline("BIS ENDE FEBRUAR 2027", REF).day == date(2027, 2, 28)
    assert parse_deadline("AB JANUAR 2027", REF).day == date(2027, 1, 1)
    assert parse_deadline("ZUM 01.12.2026", REF).day == date(2026, 12, 1)
    assert parse_deadline("AB 2027", REF) is None and parse_deadline("", REF) is None
    assert deadline_of({"date_label": "BIS 30. NOVEMBER 2026"}, REF).day == date(2026, 11, 30)
    assert deadline_of(None, REF) is None


def test_termin_vor_der_frist() -> None:
    free = ["2026-11-26T16:00:00Z", "2026-11-27T16:00:00Z", "2026-11-28T10:00:00Z"]
    assert choose_slot(free, None, NOW).slot == free[0]
    ok = choose_slot(free, parse_deadline("BIS 30. NOVEMBER 2026", REF), NOW)
    assert ok.slot == free[0] and ok.warning is None                       # 26.11. ≤ 27.11.
    late = choose_slot(["2026-11-29T10:00:00Z"], parse_deadline("BIS 30. NOVEMBER 2026", REF), NOW)
    assert late.slot is None and "sofort" in late.warning                   # 29.11. zu spät → sofort vorschlagen
    past = choose_slot(free, parse_deadline("BIS 20. NOVEMBER 2026", REF), NOW)
    assert past.slot == free[0] and "vorbei" in past.warning


def test_vorrat() -> None:
    times = ["2026-11-24T16:00:00Z", "2026-11-25T16:00:00Z", "2026-11-26T16:00:00Z"]
    stock = planned_stock(times, NOW)
    assert (stock.videos, stock.days) == (2, 2)
    assert "nur 2 Videos geplant" in stock_warning(stock) and "26.11. 17:00" in stock_warning(stock)
    assert stock_warning(planned_stock(times + ["2026-11-27T16:00:00Z"], NOW)) is None
    assert "leer" in stock_warning(planned_stock([], NOW))


def test_upload_plant_frist_videos_nicht_zu_spaet(capsys) -> None:
    import json
    from dataclasses import replace

    from shorts import sample_data
    from shorts.config import Settings, load_channels
    from shorts.db import Database, Status
    from shorts.upload import run_uploads

    db = Database(":memory:")
    ids = []
    for n, label in enumerate(["BIS 27. NOVEMBER 2026", "BIS 15. DEZEMBER 2026"]):
        tid = db.add_topic("de", f"https://a.de/{n}", "Q", f"T{n}")
        db.transition(tid, Status.SKRIPT, script_json=json.dumps({**sample_data.SAMPLE_SCRIPT, "date_label": label}))
        db.transition(tid, Status.VIDEO, video_path="/tmp/x.mp4")
        db.transition(tid, Status.WARTET)
        db.transition(tid, Status.FREIGEGEBEN)
        ids.append(tid)
    ch = replace(load_channels()["de"], publish_slots=["Sun 11:00"])  # nächster Termin So 29.11. – zu spät für 27.11.
    settings = replace(Settings(), telegram_bot_token=None, telegram_chat_id=None)
    run_uploads(settings, db, ch, dry_run=True, now=NOW)
    out = capsys.readouterr().out
    assert f"#{ids[0]} →" not in out                         # Frist 27.11.: kein Termin ≤ 24.11. → nicht eingeplant
    assert f"#{ids[1]} → 2026-11-29T10:00:00Z" in out          # Frist 15.12.: So 29.11. passt
    assert db.get(ids[0]).status == Status.FREIGEGEBEN
