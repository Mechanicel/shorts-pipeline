from __future__ import annotations

import json
from datetime import date

from shorts import sample_data
from shorts.analytics import fetch_stats, latest_stats, style_comparison
from shorts.db import Database, Status


def _uploaded(db: Database, n: int, vid: str, published: str, style: str | None = None) -> int:
    tid = db.add_topic("de", f"https://a.de/{n}", "Q", f"T{n}")
    db.transition(tid, Status.SKRIPT, script_json=json.dumps({**sample_data.SAMPLE_SCRIPT,
                                                              **({"style": style} if style else {})}), style=style)
    db.transition(tid, Status.VIDEO, video_path="/tmp/x.mp4")
    db.transition(tid, Status.WARTET)
    db.transition(tid, Status.FREIGEGEBEN)
    db.transition(tid, Status.HOCHGELADEN, youtube_id=vid, publish_at=published, yt_visibility="öffentlich")
    return tid


def test_statistiken_und_72h() -> None:
    db = Database(":memory:")
    a = _uploaded(db, 1, "AAA", "2026-11-20T16:00:00Z", "v3-merkliste")
    b = _uploaded(db, 2, "BBB", "2026-11-24T16:00:00Z", "v2-hook-frame0")
    calls = []

    def query(**p):
        calls.append(p)
        if p["dimensions"] == "video":
            assert sorted(p["filters"][7:].split(",")) == ["AAA", "BBB"]
            return {"rows": [["AAA", 500, 120, 9.5, 88.0, 3, 1, 2], ["BBB", 40, 10, 20.0, 70.0, 0, 0, 0]]}
        assert p["filters"] == "video==AAA" and p["startDate"] == "2026-11-20" and p["endDate"] == "2026-11-22"
        return {"rows": [["2026-11-20", 300], ["2026-11-22", 50]]}  # Tag ohne Aufrufe fehlt

    counts = fetch_stats(db, "de", query, today=date(2026, 11, 25))
    assert counts == {"videos": 2, "rows": 2, "h72": 1}
    assert db.get(a).yt_views_72h == "350" and db.get(b).yt_views_72h is None  # B ist erst 1 Tag alt
    stats = latest_stats(db)
    assert stats[a]["views"] == 500 and stats[a]["avg_view_pct"] == 88.0 and stats[b]["engaged_views"] == 10
    fetch_stats(db, "de", query, today=date(2026, 11, 25))
    assert sum(1 for c in calls if c["dimensions"] == "day") == 1  # 72-h-Wert wird nur einmal geholt

    rows = {r["style"]: r for r in style_comparison(db, "de", duration_of=lambda p: 10.0)}
    assert rows["v3-merkliste"]["avg_72h"] == 350 and rows["v3-merkliste"]["avg_len"] == 10.0
    assert rows["v2-hook-frame0"]["avg_72h"] is None and rows["v2-hook-frame0"]["videos"] == 1
