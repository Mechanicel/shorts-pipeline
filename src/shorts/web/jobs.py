"""Startet CLI-Befehle (pipeline, upload, …) als Hintergrundprozess und protokolliert sie."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..db import Database
from ..notify import notify

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, settings: Settings, db_factory: Callable[[], Database]) -> None:
        self.s, self._db_factory = settings, db_factory
        self._lock = threading.Lock()
        self._current: dict | None = None
        self.log_dir = settings.data_dir / "logs"

    def running(self) -> dict | None:
        with self._lock:
            cur = dict(self._current) if self._current else None
        if cur:
            cur["step"] = progress(cur["kind"], cur.get("log_path"))
        return cur

    def launch(self, kind: str, args: list[str], trigger: str) -> int | None:
        """Startet den Job; None, wenn schon einer läuft (die CLI sperrt zusätzlich per flock)."""
        with self._lock:
            if self._current:
                log.warning("Job %s übersprungen – %s läuft noch", kind, self._current["kind"])
                return None
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{kind}.log"
            job_id = self._db_factory().start_job(kind, args, trigger, str(log_path))
            self._current = {"id": job_id, "kind": kind, "trigger": trigger, "log_path": str(log_path),
                             "started": datetime.now().strftime("%H:%M:%S")}
        threading.Thread(target=self._run, args=(job_id, kind, args, log_path), daemon=True).start()
        return job_id

    def _run(self, job_id: int, kind: str, args: list[str], log_path: Path) -> None:
        rc = -1
        result_file = log_path.with_suffix(".result.json")
        env = {**os.environ, "SHORTS_RESULT_FILE": str(result_file)}
        try:
            with log_path.open("w", encoding="utf-8") as fh:
                rc = subprocess.run([sys.executable, "-m", "shorts", *args], stdout=fh, env=env,
                                    stderr=subprocess.STDOUT, cwd=str(self.s.root)).returncode
        except Exception:
            log.exception("Job %s konnte nicht gestartet werden", kind)
        finally:
            status, summary = read_result(result_file, rc)
            self._db_factory().finish_job(job_id, rc, status, summary)
            with self._lock:
                self._current = None
        if rc != 0:  # die CLI meldet Ausnahmen selbst; das hier fängt alles andere ab
            notify(self.s, f"⚠️ Web-Job '{kind}' endete mit Code {rc} (Log im Webinterface, Job #{job_id}).")


def read_result(path: Path, rc: int) -> tuple[str, str]:
    """Ergebnis aus SHORTS_RESULT_FILE; ein Exit-Code ≠ 0 ist immer ein technischer Fehler."""
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    if rc != 0:
        return "error", data.get("summary") or f"Abbruch mit Code {rc} – siehe Log"
    return data.get("status", "ok"), data.get("summary", "fertig")


PIPELINE_STEPS = [  # (Log-Merkmal, Anzeige) – Reihenfolge = Ablauf
    ("shorts.sources", "1/5 Quellen abrufen"),
    ("shorts.script", "2/5 Skript schreiben (Claude)"),
    ("shorts.tts", "3/5 Stimme erzeugen"),
    ("shorts.subtitles", "4/5 Untertitel & Hintergrund"),
    ("shorts.backgrounds", "4/5 Untertitel & Hintergrund"),
    ("shorts.render", "5/5 Video rendern"),
]


def progress(kind: str, log_path: str | None) -> str:
    """Grober Fortschritt eines laufenden Jobs aus seinem Log."""
    if kind != "pipeline" or not log_path or not Path(log_path).exists():
        return ""
    text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    step = ""
    for line in text.splitlines():
        for marker, label in PIPELINE_STEPS:
            if marker in line:
                step = label
    return step


def tail(path: str | None, lines: int = 200) -> str:
    if not path or not Path(path).exists():
        return "(kein Log)"
    text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def args_label(args_json: str) -> str:
    return " ".join(json.loads(args_json))
