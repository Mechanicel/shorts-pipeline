from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from shorts.__main__ import job_lock
from shorts.config import Settings


def test_job_lock_verhindert_parallele_laeufe(tmp_path: Path) -> None:
    settings = replace(Settings(), data_dir=tmp_path)
    with job_lock(settings) as first:
        assert first is True
        with job_lock(settings) as second:  # zweiter Lauf, während der erste noch läuft
            assert second is False
    with job_lock(settings) as again:  # nach Ende des ersten wieder frei
        assert again is True
