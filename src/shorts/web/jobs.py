"""Startet CLI-Befehle (pipeline, upload, …) als Hintergrundprozess und protokolliert sie."""
from __future__ import annotations

import json
import logging
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
            return dict(self._current) if self._current else None

    def launch(self, kind: str, args: list[str], trigger: str) -> int | None:
        """Startet den Job; None, wenn schon einer läuft (die CLI sperrt zusätzlich per flock)."""
        with self._lock:
            if self._current:
                log.warning("Job %s übersprungen – %s läuft noch", kind, self._current["kind"])
                return None
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{kind}.log"
            job_id = self._db_factory().start_job(kind, args, trigger, str(log_path))
            self._current = {"id": job_id, "kind": kind, "trigger": trigger,
                             "started": datetime.now().strftime("%H:%M:%S")}
        threading.Thread(target=self._run, args=(job_id, kind, args, log_path), daemon=True).start()
        return job_id

    def _run(self, job_id: int, kind: str, args: list[str], log_path: Path) -> None:
        rc = -1
        try:
            with log_path.open("w", encoding="utf-8") as fh:
                rc = subprocess.run([sys.executable, "-m", "shorts", *args], stdout=fh,
                                    stderr=subprocess.STDOUT, cwd=str(self.s.root)).returncode
        except Exception:
            log.exception("Job %s konnte nicht gestartet werden", kind)
        finally:
            self._db_factory().finish_job(job_id, rc)
            with self._lock:
                self._current = None
        if rc != 0:  # die CLI meldet Ausnahmen selbst; das hier fängt alles andere ab
            notify(self.s, f"⚠️ Web-Job '{kind}' endete mit Code {rc} (Log im Webinterface, Job #{job_id}).")


def tail(path: str | None, lines: int = 200) -> str:
    if not path or not Path(path).exists():
        return "(kein Log)"
    text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def args_label(args_json: str) -> str:
    return " ".join(json.loads(args_json))
