"""Beispieldaten für --dry-run. Inhalt ist bewusst als Test gekennzeichnet (fiktiv)."""
from __future__ import annotations

SAMPLE_TOPIC = {
    "url": "https://www.example.org/testthema-ruecksendefrist",
    "source_name": "Beispielquelle (Dry-Run)",
    "title": "Testthema: Neue Frist für Rücksendungen",
    "summary": "Fiktives Beispielthema für den Dry-Run.",
}

SAMPLE_SOURCE_TEXT = """\
Dies ist ein fiktiver Beispieltext für den Testlauf. Ab dem 1. März gilt im Beispielland eine
verlängerte Rücksendefrist von 30 Tagen für Online-Bestellungen. Bisher lag die Frist bei 14 Tagen.
Die Regel gilt für alle Bestellungen, die ab dem Stichtag aufgegeben werden. Händler müssen die
Kosten der Rücksendung tragen, wenn der Warenwert über 40 Euro liegt. Verbraucherinnen und
Verbraucher sollten den Kaufbeleg aufbewahren, um die Frist nachweisen zu können.
"""

SAMPLE_SCRIPT = {
    "discard": False,
    "discard_reason": "",
    "title": "TEST: 30 Tage Rückgabe statt 14 – was du wissen musst",
    "overlay_title": "30 Tage Rückgabe",
    "narration": (
        "Dein Rückgaberecht wird mehr als doppelt so lang. "
        "In unserem Beispielland gilt ab dem ersten März eine Rücksendefrist von dreißig Tagen. "
        "Bisher waren es nur vierzehn. Das gilt für alle Bestellungen ab dem Stichtag. "
        "Liegt der Warenwert über vierzig Euro, zahlt der Händler das Porto. "
        "Heb deinen Kaufbeleg auf, damit du die Frist nachweisen kannst. "
        "Mehr Zeit zum Zurückschicken, und ab vierzig Euro kein Porto mehr."
    ),
    "description": "Testvideo der Pipeline mit fiktiven Beispieldaten. Kein echtes Gesetz.",
    "key_facts": [
        {"value": "30 Tage", "label": "Rücksendefrist"},
        {"value": "14 Tage", "label": "bisherige Frist"},
        {"value": "ab 40 €", "label": "Händler zahlt Porto"},
    ],
    "date_label": "AB 1. MÄRZ",
    "hashtags": ["#shorts", "#test", "#verbraucher"],
    "evidence": [
        {"claim": "Ab 1. März 30 Tage Rücksendefrist",
         "quote": "Ab dem 1. März gilt im Beispielland eine verlängerte Rücksendefrist von 30 Tagen"},
        {"claim": "Bisher 14 Tage", "quote": "Bisher lag die Frist bei 14 Tagen."},
        {"claim": "Händler zahlt Rücksendung über 40 Euro",
         "quote": "Händler müssen die Kosten der Rücksendung tragen, wenn der Warenwert über 40 Euro liegt."},
    ],
}


# Beispiel für Stil v3 „Merkliste“ (fiktives Beispielland, nur Dry-Run und Tests)
SAMPLE_V3 = {
    "discard": False, "discard_reason": "", "list_possible": True, "list_impossible_reason": "",
    "title": "Ab 1. März: 4 Änderungen bei Rücksendung, Porto & Co.",
    "headline": "AB 1. MÄRZ GILT NEU",
    "items": [
        {"icon": "package", "label": "Rücksendefrist", "old_value": "14 Tage", "new_value": "30 Tage",
         "note": "für alle Bestellungen", "status": "beschlossen",
         "evidence": "Ab dem 1. März gilt im Beispielland eine verlängerte Rücksendefrist von 30 Tagen."},
        {"icon": "euro", "label": "Porto ab Warenwert", "old_value": "", "new_value": "ab 40 €",
         "note": "zahlt der Händler", "status": "beschlossen",
         "evidence": "Liegt der Warenwert über 40 Euro, trägt der Händler die Kosten der Rücksendung."},
        {"icon": "receipt", "label": "Kaufbeleg", "old_value": "", "new_value": "aufheben",
         "note": "", "status": "in Kraft", "evidence": "Bisher lag die Frist bei 14 Tagen."},
        {"icon": "calendar", "label": "Stichtag", "old_value": "", "new_value": "1. März",
         "note": "", "status": "geplant", "evidence": "Ab dem 1. März gilt im Beispielland"},
    ],
    "date_label": "AB 1. MÄRZ", "source_label": "Quelle: Beispielministerium",
    "cta": "Schick das jemandem, der viel online bestellt.",
    "narration": "Dein Rückgaberecht wird mehr als doppelt so lang. Hier alles auf einen Blick, "
                 "was sich ab März für dich ändert.",
    "description": "Im Beispielland gelten neue Regeln für Rücksendungen.",
    "hashtags": ["#rücksendung", "#verbraucher", "#onlineshopping"],
    "background_query": "parcel",
}
