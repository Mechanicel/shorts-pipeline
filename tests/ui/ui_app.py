"""Startet das Webinterface für die UI-Prüfung auf einer KOPIE der Datenbank (eigenes Testpasswort, kein Zeitplan).

Aufruf im Container: python tests/ui/ui_app.py  → http://localhost:8090 (nur für deploy/ui_check.sh gedacht)
"""
import shutil
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, "/app/src")
import uvicorn  # noqa: E402

from shorts.config import load_settings  # noqa: E402
from shorts.db import Database  # noqa: E402
from shorts.web import auth  # noqa: E402
from shorts.web.app import create_app  # noqa: E402

PASSWORD = "ui-check-nur-lokal"

settings = load_settings()
tmp = Path("/tmp/ui-check-data")
shutil.rmtree(tmp, ignore_errors=True)
tmp.mkdir(parents=True)
src = sqlite3.connect(settings.db_path)
dst = sqlite3.connect(tmp / "pipeline.sqlite3")
src.backup(dst)  # konsistente Kopie inkl. WAL
dst.close()
src.close()
ui_settings = replace(settings, data_dir=tmp)
auth.set_password(Database(ui_settings.db_path), PASSWORD)
uvicorn.run(create_app(ui_settings, start_scheduler=False), host="127.0.0.1", port=8090, log_level="warning")
