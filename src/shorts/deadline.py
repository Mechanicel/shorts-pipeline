"""Fristen und Stichtage: Videos mit Frist ("BIS …") oder Stichtag ("AB 1. JANUAR 2027") müssen mindestens
LEAD_DAYS Tage vorher erscheinen – sonst ist das Video für die Zuschauer wertlos.

Außerdem die Vorrats-Prüfung: Wie viele Tage sind mit geplanten Videos abgedeckt?
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
LEAD_DAYS = 3
MONTHS = {"januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7,
          "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12}
_MONTH_RX = "|".join(MONTHS)
_DAY_MONTH = re.compile(rf"(\d{{1,2}})\.\s*({_MONTH_RX})(?:\s+(\d{{4}}))?", re.IGNORECASE)
_NUMERIC = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?")
_END_MONTH = re.compile(rf"ende\s+({_MONTH_RX})(?:\s+(\d{{4}}))?", re.IGNORECASE)
_END_YEAR = re.compile(r"ende\s+(\d{4})", re.IGNORECASE)
_MONTH_YEAR = re.compile(rf"\b({_MONTH_RX})\s+(\d{{4}})", re.IGNORECASE)


@dataclass(frozen=True)
class Deadline:
    day: date
    kind: str     # "Frist" (BIS …) | "Stichtag" (AB/ZUM/SEIT …)
    label: str    # Originaltext, z. B. "BIS 30. NOVEMBER 2026"

    @property
    def latest_publish_day(self) -> date:
        return self.day - timedelta(days=LEAD_DAYS)


def _year_for(month: int, day: int, explicit: str | None, ref: date) -> int:
    if explicit:
        y = int(explicit)
        return y + 2000 if y < 100 else y
    # ohne Jahr: nächstes Vorkommen ab dem Bezugsdatum (z. B. "BIS 15. JANUAR" im Dezember → nächstes Jahr)
    candidate = date(ref.year, month, min(day, calendar.monthrange(ref.year, month)[1]))
    return ref.year if candidate >= ref - timedelta(days=31) else ref.year + 1


def parse_deadline(label: str, ref: date) -> Deadline | None:
    """date_label → Deadline. Nur eindeutige Tagesangaben; vage Angaben ("2027", "BALD") ergeben None."""
    if not label:
        return None
    text = label.strip()
    low = text.lower()
    kind = "Frist" if re.search(r"\b(bis|spätestens|frist)\b", low) else "Stichtag"
    if m := _DAY_MONTH.search(text):
        d, mon = int(m.group(1)), MONTHS[m.group(2).lower()]
        y = _year_for(mon, d, m.group(3), ref)
        try:
            return Deadline(date(y, mon, d), kind, text)
        except ValueError:
            return None
    if m := _NUMERIC.search(text):
        d, mon = int(m.group(1)), int(m.group(2))
        try:
            return Deadline(date(_year_for(mon, d, m.group(3), ref), mon, d), kind, text)
        except ValueError:
            return None
    if m := _END_MONTH.search(text):
        mon = MONTHS[m.group(1).lower()]
        y = _year_for(mon, 1, m.group(2), ref)
        return Deadline(date(y, mon, calendar.monthrange(y, mon)[1]), "Frist", text)
    if m := _END_YEAR.search(text):
        return Deadline(date(int(m.group(1)), 12, 31), "Frist", text)
    if kind == "Stichtag" and (m := _MONTH_YEAR.search(text)):  # "AB JANUAR 2027" → 1. Januar
        return Deadline(date(int(m.group(2)), MONTHS[m.group(1).lower()], 1), kind, text)
    return None


def deadline_of(script: dict | None, ref: date) -> Deadline | None:
    return parse_deadline((script or {}).get("date_label", ""), ref)


@dataclass(frozen=True)
class SlotChoice:
    slot: str | None          # UTC-ISO oder None = sofort vorschlagen
    warning: str | None       # Text für Web/Telegram, wenn die Frist den Termin bestimmt


def choose_slot(free_slots: list[str], deadline: Deadline | None, now: datetime) -> SlotChoice:
    """free_slots: freie Termine aufsteigend (UTC-ISO). Ohne Frist: der erste. Mit Frist: der erste freie Termin,
    der spätestens LEAD_DAYS vor der Frist liegt; gibt es keinen → None (= sofort) plus Warnung."""
    first = free_slots[0] if free_slots else None
    if deadline is None:
        return SlotChoice(first, None)
    today = now.astimezone(BERLIN).date()
    when = f"{deadline.kind} {deadline.day:%d.%m.%Y}"
    if deadline.day < today:
        return SlotChoice(first, f"{when} ist schon vorbei – Thema prüfen, bevor es erscheint.")
    for slot in free_slots:
        local = datetime.fromisoformat(slot.replace("Z", "+00:00")).astimezone(BERLIN).date()
        if local <= deadline.latest_publish_day:
            return SlotChoice(slot, None)
        break  # Termine sind aufsteigend – der erste ist der früheste
    return SlotChoice(None, f"{when}: kein freier Termin mindestens {LEAD_DAYS} Tage vorher – "
                            "Vorschlag: sofort veröffentlichen.")


# ------------------------------------------------------------------ Vorrat
@dataclass(frozen=True)
class Stock:
    videos: int
    days: int
    last: datetime | None


def planned_stock(publish_ats: list[str], now: datetime) -> Stock:
    """Zukünftige Termine → Anzahl Videos, Anzahl abgedeckter Tage, letzter Termin."""
    future = sorted(datetime.fromisoformat(p.replace("Z", "+00:00")) for p in publish_ats if p)
    future = [p for p in future if p > now]
    days = {p.astimezone(BERLIN).date() for p in future}
    return Stock(len(future), len(days), future[-1] if future else None)


def stock_warning(stock: Stock, min_days: int = 3) -> str | None:
    if stock.days >= min_days:
        return None
    if not stock.last:
        return "📦 Vorrat leer: Es ist kein Video mehr geplant. Neue Videos erstellen und freigeben."
    last = stock.last.astimezone(BERLIN)
    return (f"📦 Vorrat knapp: nur {stock.videos} {'Video' if stock.videos == 1 else 'Videos'} geplant "
            f"(letzter Termin {last:%d.%m. %H:%M}). Neue Videos erstellen und freigeben.")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
