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
from .costs import per_minute_cost
from .db import Database, Status, Topic
from .notify import notify
from .backgrounds import pick_background
from .render import RenderJob, probe_duration, render
from .script import (ClaudeScriptWriter, SampleScriptWriter, ScriptWriter, script_to_json,
                     unverified_numbers, unverified_quotes)
from .result import ERROR, OK, WARN, plural, report as report_result
from .sources import collect_topics, fetch_article
from .triage import Candidate, HaikuTriage, PassAllTriage, Triage, prefilter_reason
from .subtitles import estimate_timings, split_words, whisper_timings
from .tts import TTSEngine, apply_speed, get_tts

log = logging.getLogger(__name__)

STALE_AFTER_DAYS = 21       # neu-Themen, die so lange liegen bleiben, werden verworfen
MAX_SOURCE_AGE_DAYS = 21    # Quelle (Artikeldatum) darf höchstens so alt sein


def source_age_problem(date_str: str | None, today: datetime) -> str | None:
    """None = ok, sonst Verwerfungsgrund. date_str: ISO-Datum oder -Zeitstempel der Quelle."""
    if not date_str:
        return "kein Datum (Ratgeber ohne Datum)"
    d = datetime.fromisoformat(date_str[:10]).date()
    age = (today.date() - d).days
    if age > MAX_SOURCE_AGE_DAYS:
        return f"zu alt (Quelle vom {d:%d.%m.%Y}, {age} Tage)"
    return None


@dataclass
class RunReport:
    new_topics: int = 0
    scripted: int = 0
    discarded: int = 0
    rendered: int = 0
    errors: list[str] = field(default_factory=list)
    videos: list[Path] = field(default_factory=list)
    cost_usd: float = 0.0
    eur_per_usd: float = 0.9
    discard_reasons: list[str] = field(default_factory=list)
    discard_examples: list[str] = field(default_factory=list)
    checked: int = 0  # Themen, die das Skript-Modell bekommen hat

    def result(self) -> tuple[str, str]:
        """(Status, Zusammenfassung) für result.report – ✅ nur, wenn Videos entstanden sind."""
        from collections import Counter
        why = ", ".join(f"{n}× {r}" for r, n in Counter(self.discard_reasons).items())
        if not self.rendered and not self.errors:
            if not self.discarded and not self.checked:
                return WARN, "Kein Video: keine offenen Themen – neue Meldungen der Quellen abwarten."
            return WARN, f"Kein Video: {plural(self.discarded, 'Thema', 'Themen')} geprüft und verworfen ({why})."
        text = (f"{plural(self.rendered, 'Video', 'Videos')} erstellt, {self.discarded} verworfen"
                + (f" ({why})" if why else "") + f", {plural(len(self.errors), 'Fehler', 'Fehler')}")
        if self.rendered and not self.errors:
            return OK, text
        if self.rendered:
            return WARN, text
        return ERROR, text

    def summary(self, channel: str) -> str:
        s = (f"Pipeline [{channel}]: {self.new_topics} neue Themen, {self.scripted} Skripte, "
             f"{self.discarded} verworfen, {self.rendered} Videos gerendert.")
        if self.cost_usd:
            eur = f"{self.cost_usd * self.eur_per_usd:.2f}".replace(".", ",")
            s += f"\n💶 Kosten dieses Laufs: ca. {eur} € (inkl. verworfener Themen)"
        if self.errors:
            s += f"\n⚠️ {len(self.errors)} Fehler:\n- " + "\n- ".join(self.errors[:10])
        return s


class Pipeline:
    def __init__(self, settings: Settings, channel: ChannelConfig, db: Database, *,
                 dry_run: bool = False, writer: ScriptWriter | None = None,
                 tts: TTSEngine | None = None, triage: Triage | None = None) -> None:
        self.s = settings
        self.ch = channel
        self.db = db
        self.dry_run = dry_run
        self.writer = writer or (SampleScriptWriter(sample_data.SAMPLE_SCRIPT) if dry_run
                                 else ClaudeScriptWriter(settings, channel))
        self.tts = tts or get_tts(settings, channel, dry_run=dry_run)
        self._run_cost = 0.0
        self.triage = triage or (PassAllTriage() if dry_run else HaikuTriage(settings, channel))

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

    def _discard(self, topic: Topic, report: RunReport, category: str, note: str) -> None:
        self.db.transition(topic.id, Status.VERWORFEN, note=note)
        log.info("Thema %s verworfen (%s): %s", topic.id, category, note)
        report.discarded += 1
        report.discard_reasons.append(category)
        report.discard_examples.append(f"#{topic.id} {topic.title[:60]} – {note}")

    def step_script(self, report: RunReport) -> None:
        target = self.ch.max_videos_per_run - len(self.db.by_status(Status.SKRIPT, self.ch.key))
        if target <= 0:
            return
        # Per 'regenerate' neu eingereihte Themen zuerst (prev_status gesetzt) – sie überspringen Filter/Vorauswahl
        queue = sorted(self.db.by_status(Status.NEU, self.ch.key), key=lambda t: t.prev_status is None)

        # Stufe 1: Vorfilter (kostenlos)
        kept: list[Topic] = []
        for t in queue:
            term = None if t.prev_status else prefilter_reason(t.title, self.ch.title_blocklist)
            if term:
                self._discard(t, report, "Vorfilter (Diplomatie/Termine)", f"Vorfilter: Titel enthält „{term}“")
            else:
                kept.append(t)

        # Stufe 2: Vorauswahl mit günstigem Modell (nur Titel + Anriss, ein Aufruf)
        to_screen = [t for t in kept if t.triage is None and not t.prev_status][: self.s.triage_max]
        if to_screen:
            try:
                verdicts = self.triage.screen([Candidate(t.id, t.title, t.summary or "") for t in to_screen])
            except Exception as exc:  # Vorauswahl ist eine Sparmaßnahme – bei Ausfall alles durchlassen
                log.warning("Vorauswahl fehlgeschlagen (%s) – Themen ohne Vorauswahl weiter", type(exc).__name__)
                verdicts = {t.id: (True, "Vorauswahl ausgefallen") for t in to_screen}
            finally:
                spent = sum(x or 0.0 for x in getattr(self.triage, "spent", []))
                report.cost_usd += spent
                for t in to_screen:  # Kosten anteilig auf die geprüften Themen verteilen
                    self.db.add_cost(t.id, "vorauswahl", spent / len(to_screen))
            for t in to_screen:
                ok, why = verdicts[t.id]
                if ok:
                    self.db.update_fields(t.id, triage=f"ok: {why}")
                else:
                    self.db.update_fields(t.id, triage=why)
                    self._discard(t, report, "Vorauswahl: kein Verbraucherbezug", f"Vorauswahl: {why}")

        # Stufe 3: Skript mit dem großen Modell – nur für Themen, die die Vorauswahl bestanden haben
        budget_calls = self.ch.max_new_topics_per_run
        ready = [self.db.get(t.id) for t in kept]
        ready = [t for t in ready if t.status == Status.NEU and (t.prev_status or (t.triage or "").startswith("ok"))]
        for topic in ready:
            if budget_calls <= 0 or report.scripted >= target:
                break
            budget_calls -= 1
            report.checked += 1
            try:
                self._script_one(topic, report)
            except Exception as exc:
                self._fail(topic, "Skript", exc, report)

    def _script_one(self, topic: Topic, report: RunReport) -> None:
        text, date_str = topic.source_text, topic.published_at
        if not text or not date_str:  # Datum notfalls von der Artikelseite (z. B. Verbraucherzentrale)
            article = fetch_article(topic.url)
            text, date_str = text or article.text, date_str or article.date
            self.db.update_fields(topic.id, source_text=text, published_at=date_str)
        if not self.dry_run and (problem := source_age_problem(date_str, datetime.now(timezone.utc))):
            self._discard(topic, report, problem.split(" (")[0], problem)  # vor Claude → kostet nichts
            return
        if len(text) < 300:
            self.db.transition(topic.id, Status.VERWORFEN, source_text=text, note="Quelltext zu kurz")
            report.discarded += 1
            return
        try:
            script = self.writer.write(headline=topic.title, url=topic.url, source_name=topic.source_name,
                                       text=text, source_date=(date_str or "unbekannt")[:10])
        finally:  # auch fehlgeschlagene/verworfene Versuche kosten Geld
            for usd in getattr(self.writer, "spent", []):
                self.db.add_cost(topic.id, "claude", usd)
                report.cost_usd += usd or 0.0
        if script.discard:
            self.db.update_fields(topic.id, source_text=text)
            self._discard(topic, report, "Skript-Prüfung: kein passendes Thema", f"Claude: {script.discard_reason}")
            return
        warnings = []
        if missing := unverified_quotes(script, text):
            warnings.append("Zitate nicht wörtlich im Quelltext: " + " | ".join(missing))
        if numbers := unverified_numbers(script, text):
            warnings.append("Grafik-Zahlen nicht im Quelltext: " + " | ".join(numbers))
        note = "\n⚠️ ".join(warnings) or None
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
        if self.tts.name == "openai":
            self._spend(topic.id, "tts", per_minute_cost(probe_duration(audio), self.s.tts_usd_per_min))
        if abs(self.s.tts_speed - 1.0) > 0.01:  # vor Whisper, damit die Zeitstempel passen
            audio = apply_speed(audio, self.s.tts_speed)
        self.db.update_fields(topic.id, audio_path=str(audio))
        duration = probe_duration(audio)
        words = split_words(narration)
        if self.s.subtitle_timing == "whisper" and not self.dry_run and self.s.openai_api_key:
            timings = whisper_timings(audio, words, self.s.openai_api_key, self.ch.language, duration)
            self._spend(topic.id, "whisper", per_minute_cost(duration, self.s.whisper_usd_per_min))
        else:
            timings = estimate_timings(words, 0.15, max(duration - 0.25, 0.5))
        (out_dir / "timings.json").write_text(
            json.dumps([t.__dict__ for t in timings], ensure_ascii=False), encoding="utf-8")
        bg = pick_background(
            assets_dir=self.s.assets_dir, cache_dir=self.s.data_dir / "backgrounds", seed=topic.id,
            pixabay_api_key=None if self.dry_run else self.s.pixabay_api_key,
            queries=self.ch.background_queries)
        self.db.update_fields(topic.id, background_credit=bg.credit)
        return render(RenderJob(
            audio=audio, timings=timings, title=script.get("overlay_title") or script["title"],
            source_url=topic.url, outro=self.ch.outro_text, out_dir=out_dir,
            background=bg.path, key_facts=script.get("key_facts") or [],
            date_label=script.get("date_label") or "",
            badge="TESTVIDEO · DRY-RUN" if self.dry_run else None))

    def _spend(self, topic_id: int, part: str, usd: float) -> None:
        self.db.add_cost(topic_id, part, usd)
        self._run_cost += usd

    def _fail(self, topic: Topic, stage: str, exc: Exception, report: RunReport) -> None:
        log.exception("%s für Thema %s fehlgeschlagen", stage, topic.id)
        msg = f"#{topic.id} {stage}: {type(exc).__name__}: {exc}"
        report.errors.append(msg[:300])
        self.db.transition(topic.id, Status.FEHLER, note=msg[:1000])

    # ------------------------------------------------------------------ run
    def run(self) -> RunReport:
        report = RunReport(eur_per_usd=self.s.eur_per_usd)
        self._run_cost = 0.0
        self.step_collect(report)
        self.step_script(report)
        self.step_render(report)
        report.cost_usd += self._run_cost
        report_result(*report.result())
        for example in report.discard_examples[:8]:  # Begründungen im Job-Log/Detail
            print(f"  verworfen: {example}", flush=True)
        log.info(report.summary(self.ch.key))
        if not self.dry_run:
            notify(self.s, report.summary(self.ch.key)
                   + ("\nDie Videos kommen gleich zur Freigabe." if report.rendered else ""))
        return report
