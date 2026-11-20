"""Wöchentlicher Pipeline-Lauf: Quellen → Skript → TTS → Video (→ Review über den Bot)."""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import sample_data
from .config import ChannelConfig, Settings
from .db import Database, Status, Topic
from .notify import notify
from .render import RenderJob, pick_background, probe_duration, render
from .script import (ClaudeScriptWriter, SampleScriptWriter, ScriptWriter, script_to_json,
                     unverified_quotes)
from .sources import collect_topics, fetch_article_text
from .subtitles import estimate_timings, split_words, whisper_timings
from .tts import TTSEngine, get_tts

log = logging.getLogger(__name__)

STALE_AFTER_DAYS = 21


@dataclass
class RunReport:
    new_topics: int = 0
    scripted: int = 0
    discarded: int = 0
    rendered: int = 0
    errors: list[str] = field(default_factory=list)
    videos: list[Path] = field(default_factory=list)

    def summary(self, channel: str) -> str:
        s = (f"Pipeline [{channel}]: {self.new_topics} neue Themen, {self.scripted} Skripte, "
             f"{self.discarded} verworfen, {self.rendered} Videos gerendert.")
        if self.errors:
            s += f"\n⚠️ {len(self.errors)} Fehler:\n- " + "\n- ".join(self.errors[:10])
        return s


class Pipeline:
    def __init__(self, settings: Settings, channel: ChannelConfig, db: Database, *,
                 dry_run: bool = False, writer: ScriptWriter | None = None,
                 tts: TTSEngine | None = None) -> None:
        self.s = settings
        self.ch = channel
        self.db = db
        self.dry_run = dry_run
        self.writer = writer or (SampleScriptWriter(sample_data.SAMPLE_SCRIPT) if dry_run
                                 else ClaudeScriptWriter(settings, channel))
        self.tts = tts or get_tts(settings, channel, dry_run=dry_run)

    # ------------------------------------------------------------------ steps
    def step_collect(self, report: RunReport) -> None:
        if self.dry_run:
            t = sample_data.SAMPLE_TOPIC
            tid = self.db.add_topic(self.ch.key, t["url"], t["source_name"], t["title"], t["summary"])
            if tid:
                self.db.update_fields(tid, source_text=sample_data.SAMPLE_SOURCE_TEXT)
                report.new_topics += 1
            return
        new, errors = collect_topics(self.db, self.ch)
        report.new_topics = new
        report.errors += [f"Quelle {e}" for e in errors]
        self._expire_stale()

    def _expire_stale(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS)).isoformat()
        for t in self.db.by_status(Status.NEU, self.ch.key):
            if t.created_at < cutoff:
                self.db.transition(t.id, Status.VERWORFEN, note="veraltet")

    def step_script(self, report: RunReport) -> None:
        budget_calls = self.ch.max_new_topics_per_run
        target = self.ch.max_videos_per_run - len(self.db.by_status(Status.SKRIPT, self.ch.key))
        for topic in self.db.by_status(Status.NEU, self.ch.key):
            if budget_calls <= 0 or report.scripted >= target:
                break
            budget_calls -= 1
            try:
                self._script_one(topic, report)
            except Exception as exc:
                self._fail(topic, "Skript", exc, report)

    def _script_one(self, topic: Topic, report: RunReport) -> None:
        text = topic.source_text or fetch_article_text(topic.url)
        if len(text) < 300:
            self.db.transition(topic.id, Status.VERWORFEN, source_text=text, note="Quelltext zu kurz")
            report.discarded += 1
            return
        script = self.writer.write(headline=topic.title, url=topic.url,
                                   source_name=topic.source_name, text=text)
        if script.discard:
            self.db.transition(topic.id, Status.VERWORFEN, source_text=text,
                               note=f"Claude: {script.discard_reason}")
            report.discarded += 1
            return
        missing = unverified_quotes(script, text)
        note = ("Zitate nicht wörtlich im Quelltext: " + " | ".join(missing)) if missing else None
        self.db.transition(topic.id, Status.SKRIPT, source_text=text,
                           script_json=script_to_json(script), note=note)
        report.scripted += 1

    def step_render(self, report: RunReport) -> None:
        for topic in self.db.by_status(Status.SKRIPT, self.ch.key, limit=self.ch.max_videos_per_run):
            try:
                path = self._render_one(topic)
                self.db.transition(topic.id, Status.VIDEO, video_path=str(path))
                report.rendered += 1
                report.videos.append(path)
            except Exception as exc:
                self._fail(topic, "Render", exc, report)

    def _render_one(self, topic: Topic) -> Path:
        script = topic.script or {}
        out_dir = self.s.output_dir / self.ch.key / f"{topic.id:05d}"
        if out_dir.exists():
            shutil.rmtree(out_dir)  # nur eigene, generierte Zwischendateien dieses Themas
        out_dir.mkdir(parents=True)
        narration: str = script["narration"]
        audio = self.tts.synthesize(narration, out_dir)
        self.db.update_fields(topic.id, audio_path=str(audio))
        duration = probe_duration(audio)
        words = split_words(narration)
        if self.s.subtitle_timing == "whisper" and not self.dry_run and self.s.openai_api_key:
            timings = whisper_timings(audio, words, self.s.openai_api_key, self.ch.language, duration)
        else:
            timings = estimate_timings(words, 0.15, max(duration - 0.25, 0.5))
        (out_dir / "timings.json").write_text(
            json.dumps([t.__dict__ for t in timings], ensure_ascii=False), encoding="utf-8")
        return render(RenderJob(
            audio=audio, timings=timings, title=script.get("overlay_title") or script["title"],
            source_url=topic.url, outro=self.ch.outro_text, out_dir=out_dir,
            background=pick_background(self.s.assets_dir, topic.id),
            badge="TESTVIDEO · DRY-RUN" if self.dry_run else None))

    def _fail(self, topic: Topic, stage: str, exc: Exception, report: RunReport) -> None:
        log.exception("%s für Thema %s fehlgeschlagen", stage, topic.id)
        msg = f"#{topic.id} {stage}: {type(exc).__name__}: {exc}"
        report.errors.append(msg[:300])
        self.db.transition(topic.id, Status.FEHLER, note=msg[:1000])

    # ------------------------------------------------------------------ run
    def run(self) -> RunReport:
        report = RunReport()
        self.step_collect(report)
        self.step_script(report)
        self.step_render(report)
        log.info(report.summary(self.ch.key))
        if not self.dry_run:
            notify(self.s, report.summary(self.ch.key)
                   + ("\nDie Videos kommen gleich zur Freigabe." if report.rendered else ""))
        return report
