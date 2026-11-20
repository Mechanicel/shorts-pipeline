"""Zeitplan (im Webinterface änderbar, in SQLite gespeichert) + APScheduler im Web-Dienst."""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import ChannelConfig
from ..db import Database
from ..upload import BERLIN, parse_slot

log = logging.getLogger(__name__)

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = dict(zip(DAYS, ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]))
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def default_schedule(channel: ChannelConfig) -> dict[str, Any]:
    # Entspricht den bisherigen systemd-Timern, aber AUS, bis diese deaktiviert sind (sonst doppelte Läufe)
    return {
        "pipeline": {"enabled": False, "days": ["sun"], "time": "06:00", "limit": channel.max_videos_per_run},
        "upload": {"enabled": False, "time": "09:30"},
        "publish_slots": list(channel.publish_slots),
    }


def load_schedule(db: Database, channel: ChannelConfig) -> dict[str, Any]:
    stored = db.get_setting(f"schedule_{channel.key}")
    sched = default_schedule(channel)
    if stored:
        for k in ("pipeline", "upload"):
            sched[k].update(stored.get(k, {}))
        sched["publish_slots"] = stored.get("publish_slots", sched["publish_slots"])
    return sched


def validate_schedule(s: dict[str, Any]) -> list[str]:
    problems = []
    p, u = s["pipeline"], s["upload"]
    if not p["days"] or any(d not in DAYS for d in p["days"]):
        problems.append("Videos erstellen: mindestens einen Wochentag wählen.")
    for name, t in (("Videos erstellen", p["time"]), ("Upload", u["time"])):
        if not _TIME.match(t or ""):
            problems.append(f"{name}: Uhrzeit im Format HH:MM angeben.")
    if not 1 <= int(p["limit"]) <= 20:
        problems.append("Videos pro Lauf: 1–20.")
    if not s["publish_slots"]:
        problems.append("Mindestens ein Veröffentlichungstermin.")
    for slot in s["publish_slots"]:
        try:
            parse_slot(slot)
        except Exception:
            problems.append(f"Termin '{slot}' ungültig (Format z. B. 'Mon 17:00').")
    return problems


def save_schedule(db: Database, channel: ChannelConfig, s: dict[str, Any]) -> list[str]:
    problems = validate_schedule(s)
    if not problems:
        db.set_setting(f"schedule_{channel.key}", s)
    return problems


def trigger_for(days: list[str], time: str) -> CronTrigger:
    h, m = time.split(":")
    return CronTrigger(day_of_week=",".join(days), hour=int(h), minute=int(m), timezone=BERLIN)


class Scheduler:
    """Plant pipeline/upload gemäß gespeichertem Zeitplan; startet sie über den JobRunner."""

    def __init__(self, db_factory: Callable[[], Database], channel: ChannelConfig,
                 launch: Callable[[str, list[str], str], Any]) -> None:
        self._db_factory, self._channel, self._launch = db_factory, channel, launch
        self._sched = BackgroundScheduler(timezone=BERLIN)

    def start(self) -> None:
        self._sched.start()
        self.apply()

    def shutdown(self) -> None:
        self._sched.shutdown(wait=False)

    def apply(self) -> None:
        """Jobs nach (geändertem) Zeitplan neu anlegen."""
        s = load_schedule(self._db_factory(), self._channel)
        self._sched.remove_all_jobs()
        ch = self._channel.key
        if s["pipeline"]["enabled"]:
            self._sched.add_job(self._launch, trigger_for(s["pipeline"]["days"], s["pipeline"]["time"]),
                                args=["pipeline", ["pipeline", "--channel", ch, "--limit", str(s["pipeline"]["limit"])],
                                      "zeitplan"], id="pipeline", misfire_grace_time=3600, coalesce=True)
        if s["upload"]["enabled"]:
            self._sched.add_job(self._launch, trigger_for(DAYS, s["upload"]["time"]),
                                args=["upload", ["upload", "--channel", ch], "zeitplan"],
                                id="upload", misfire_grace_time=3600, coalesce=True)
        log.info("Zeitplan angewendet: %s", self.next_runs())

    def next_runs(self) -> dict[str, str]:
        """Nächste Läufe (deutscher Wochentag); leer, solange der Scheduler nicht läuft."""
        out = {}
        for j in self._sched.get_jobs():
            nrt = getattr(j, "next_run_time", None)  # erst nach start() gesetzt
            if nrt:
                local = nrt.astimezone(BERLIN)
                out[j.id] = f"{DAY_LABELS[DAYS[local.weekday()]]} {local:%d.%m. %H:%M}"
        return out
