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
        "Du hast online bestellt und willst zurückschicken? Dann hast du bald mehr Zeit. "
        "Ab dem ersten März gilt in unserem Beispielland eine Rücksendefrist von dreißig Tagen. "
        "Bisher waren es nur vierzehn Tage. Die neue Regel gilt für alle Bestellungen, "
        "die ab dem Stichtag aufgegeben werden. Und es wird noch besser: Liegt der Warenwert "
        "über vierzig Euro, muss der Händler die Kosten für die Rücksendung tragen. "
        "Wichtig für dich: Heb den Kaufbeleg auf, damit du die Frist im Zweifel nachweisen kannst. "
        "Kurz gesagt: Mehr Zeit zum Zurückschicken, und ab vierzig Euro zahlst du das Porto nicht."
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
