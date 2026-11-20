"""Strukturiertes Job-Ergebnis: ok / warn / error + lesbare Zusammenfassung.

Exit-Codes bleiben für technische Fehler reserviert. Ein Befund ("0 von 1 hochgeladen – Kontingent")
ist kein Absturz: Code 0, Status warn. Das Webinterface liest das Ergebnis über SHORTS_RESULT_FILE.
"""
from __future__ import annotations

import json
import os

OK, WARN, ERROR = "ok", "warn", "error"


def report(status: str, summary: str) -> None:
    print(f"ERGEBNIS [{status}]: {summary}", flush=True)
    path = os.environ.get("SHORTS_RESULT_FILE")
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"status": status, "summary": summary}, fh, ensure_ascii=False)


def plural(n: int, singular: str, plural_form: str) -> str:
    return f"{n} {singular if n == 1 else plural_form}"
