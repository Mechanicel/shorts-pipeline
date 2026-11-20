"""FastAPI-App: Übersicht, Freigabe, Upload, Zeitplan, Job-Logs. Nur fürs Heimnetz gedacht."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import json
import re

from fastapi import FastAPI, Form, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import actions
from ..config import Settings, get_channel, load_settings
from ..costs import format_cost
from ..db import Database, Status
from ..script import word_count
from ..review_bot import source_date_label
from ..upload import (BERLIN, MIN_LEAD, STUDIO_URL, UPLOAD_COST, WOCHENTAGE, parse_publish_at, quota_status,
                      upcoming_free_slots)
from . import auth
from .jobs import JobRunner, args_label, tail
from .schedule import DAY_LABELS, DAYS, Scheduler, load_schedule, save_schedule

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
PUBLIC_PATHS = {"/login", "/setup", "/healthz"}


def local_time(iso: str | None) -> str:
    if not iso:
        return "–"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(BERLIN)
    return f"{WOCHENTAGE[dt.weekday()]} {dt:%d.%m. %H:%M}"


def job_label(args_json: str) -> str:
    """CLI-Argumente → lesbarer Name ('Upload · sofort · #623')."""
    a = json.loads(args_json)
    kind = a[0] if a else "?"
    if kind == "pipeline":
        n = a[a.index("--limit") + 1] if "--limit" in a else "?"
        return f"Videos erstellen ({n})"
    if kind == "upload":
        how = "sofort" if "--sofort" in a else ("Wunschtermin" if "--publish-at" in a else "nächste Termine")
        only = f" · #{a[a.index('--only') + 1]}" if "--only" in a else ""
        return f"Upload · {how}{only}"
    return {"check-public": "Sichtbarkeit prüfen"}.get(kind, " ".join(a))


def duration(job: dict) -> str:
    if not job.get("finished_at"):
        return "läuft"
    secs = int((datetime.fromisoformat(job["finished_at"]) - datetime.fromisoformat(job["started_at"])).total_seconds())
    return f"{secs // 60}:{secs % 60:02d} min"


def linkify_topics(text: str) -> str:
    """'#123' im (bereits escapten) Log → Link zur Karte in der Übersicht."""
    return re.sub(r"#(\d{1,6})\b", r'<a href="/#t\1">#\1</a>', text)


_PLAN_WORDS = re.compile(r"\b(Gesetzentwurf|Referentenentwurf|Entwurf|Eckpunkte|geplant|plant|soll(en)? künftig)\b", re.I)
_HEDGE_WORDS = re.compile(r"\b(soll|sollen|geplant|plant|Entwurf|voraussichtlich|könnte|kann)\b", re.I)


def warning_badges(topic, today=None) -> list[str]:
    """Auffällige Hinweise oben auf der Freigabe-Karte (Faktencheck-Hilfe)."""
    from datetime import date
    badges = []
    if topic.note:
        if "Zitate" in topic.note:
            badges.append("Zitat nicht wörtlich belegt")
        if "Grafik-Zahlen" in topic.note:
            badges.append("Zahl nicht im Quelltext")
    if topic.published_at:
        age = ((today or date.today()) - datetime.fromisoformat(topic.published_at[:10]).date()).days
        if age > 7:
            badges.append(f"Quelle {age} Tage alt")
    else:
        badges.append("Datum unbekannt")
    narration = (topic.script or {}).get("narration", "")
    if topic.source_text and _PLAN_WORDS.search(topic.source_text) and not _HEDGE_WORDS.search(narration):
        badges.append("Rechtsstatus prüfen: Quelle spricht von Plan/Entwurf")
    return badges


def plural_de(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


TEMPLATES.env.filters["local_time"] = local_time
TEMPLATES.env.filters["args_label"] = args_label
TEMPLATES.env.filters["job_label"] = job_label
TEMPLATES.env.globals.update(duration=duration, warning_badges=warning_badges, plural=plural_de)


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    s = settings or load_settings()
    channel = get_channel(s.default_channel)

    def db() -> Database:  # eine Verbindung pro Request/Thread (sqlite3 ist nicht threadsicher)
        return Database(s.db_path)

    runner = JobRunner(s, db)
    scheduler = Scheduler(db, channel, runner.launch)
    throttle = auth.LoginThrottle()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            scheduler.start()
        yield
        if start_scheduler:
            scheduler.shutdown()

    app = FastAPI(title="Shorts-Pipeline", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runner, app.state.scheduler = runner, scheduler

    # ------------------------------------------------------------------ Zugriffsschutz
    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path not in PUBLIC_PATHS:
            d = db()
            if not auth.is_configured(d):
                return RedirectResponse("/setup", status_code=303)
            if not auth.valid_cookie(d, request.cookies.get(auth.COOKIE)):
                return RedirectResponse("/login", status_code=303)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        text = {404: "Seite oder Eintrag nicht gefunden.", 405: "Diese Aktion geht so nicht."}.get(
            exc.status_code, f"Fehler {exc.status_code}.")
        return TEMPLATES.TemplateResponse(request, "error.html", {"channel": channel, "running": None,
                                          "code": exc.status_code, "text": text}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        log.exception("Fehler bei %s", request.url.path)
        return TEMPLATES.TemplateResponse(request, "error.html", {"channel": channel, "running": None, "code": 500,
                                          "text": "Interner Fehler – Details stehen im Log des Web-Dienstes."},
                                          status_code=500)

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, {"channel": channel, "running": runner.running(), **ctx})

    def back(msg: str = "", err: str = "", to: str = "/") -> RedirectResponse:
        q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
        return RedirectResponse(to + q, status_code=303)

    def login_response(d: Database) -> RedirectResponse:
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(auth.COOKIE, auth.make_cookie(d), max_age=auth.SESSION_SECONDS,
                     httponly=True, samesite="strict")
        return r

    @app.get("/healthz")
    def healthz():
        return PlainTextResponse("ok")

    @app.get("/setup")
    def setup_form(request: Request):
        if auth.is_configured(db()):
            return RedirectResponse("/login", status_code=303)
        return page(request, "setup.html", min_len=auth.MIN_PASSWORD_LEN)

    @app.post("/setup")
    def setup(request: Request, password: str = Form(...), password2: str = Form(...)):
        d = db()
        if auth.is_configured(d):  # nur beim allerersten Start möglich
            return RedirectResponse("/login", status_code=303)
        if password != password2:
            return page(request, "setup.html", min_len=auth.MIN_PASSWORD_LEN, err="Passwörter stimmen nicht überein.")
        try:
            auth.set_password(d, password)
        except ValueError as exc:
            return page(request, "setup.html", min_len=auth.MIN_PASSWORD_LEN, err=str(exc))
        return login_response(d)

    @app.get("/login")
    def login_form(request: Request):
        if not auth.is_configured(db()):
            return RedirectResponse("/setup", status_code=303)
        return page(request, "login.html")

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        d, ip = db(), (request.client.host if request.client else "?")
        if throttle.locked(ip):
            return page(request, "login.html", err="Zu viele Fehlversuche – bitte 5 Minuten warten.")
        if not auth.verify_password(password, d.get_setting("password_hash", "")):
            throttle.failed(ip)
            return page(request, "login.html", err="Falsches Passwort.")
        throttle.succeeded(ip)
        return login_response(d)

    @app.post("/logout")
    def logout():
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(auth.COOKIE)
        return r

    # ------------------------------------------------------------------ Übersicht
    @app.get("/")
    def dashboard(request: Request, msg: str = "", err: str = ""):
        d = db()
        now = datetime.now(timezone.utc)
        sched = load_schedule(d, channel)
        uploaded = d.recent_topics([Status.HOCHGELADEN, Status.PAKET], 15)
        quota = quota_status(s, d, now)
        free_slots = upcoming_free_slots(sched["publish_slots"], d.taken_publish_slots(channel.key), now, 6)
        videos_30 = [t for t in d.recent_topics([Status.VIDEO, Status.WARTET, Status.FREIGEGEBEN, Status.PAKET,
                                                  Status.HOCHGELADEN], 200)
                     if t.cost_json and t.updated_at >= (now - timedelta(days=30)).isoformat()]
        from ..costs import total as cost_total
        avg_video = (sum(cost_total(t.cost_json) for t in videos_30) / len(videos_30)) if videos_30 else 0.0
        # Ob API-Uploads öffentlich werden, aus echten Prüfergebnissen ableiten (nicht behaupten)
        api_public = any(t.youtube_id and t.yt_visibility == "öffentlich" for t in uploaded)
        upload_blocked = s.upload_mode == "api" and quota["possible"] <= 0
        return page(
            request, "dashboard.html", msg=msg, err=err,
            waiting=d.by_status(Status.VIDEO, channel.key) + d.by_status(Status.WARTET, channel.key),
            free_slots=free_slots, avg_video=avg_video,
            approved=d.by_status(Status.FREIGEGEBEN, channel.key),
            uploaded=uploaded, quota=quota, api_public=api_public, upload_blocked=upload_blocked,
            upload_cost=UPLOAD_COST, source_date_label=source_date_label,
            failed=d.by_status(Status.FEHLER, channel.key),
            in_work=d.by_status(Status.SKRIPT, channel.key),
            counts=d.counts(channel.key), jobs=d.recent_jobs(10),
            cost_week=d.costs_since((now - timedelta(days=7)).isoformat()),
            cost_month=d.costs_since((now - timedelta(days=30)).isoformat()),
            eur=s.eur_per_usd, sched=sched, next_runs=scheduler.next_runs(),
            upload_mode=s.upload_mode, format_cost=format_cost, word_count=word_count,
            studio_url=STUDIO_URL, day_labels=DAY_LABELS,
            default_at=(now + MIN_LEAD).astimezone(BERLIN).strftime("%Y-%m-%dT%H:%M"),
        )

    @app.get("/media/{topic_id}.mp4")
    def media(topic_id: int):
        t = db().get(topic_id)
        if not t.video_path:
            raise HTTPException(404)
        path = Path(t.video_path).resolve()
        if not path.is_relative_to(s.output_dir.resolve()) or not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="video/mp4")

    # ------------------------------------------------------------------ Aktionen
    def start(kind: str, args: list[str], label: str) -> RedirectResponse:
        if runner.launch(kind, args, "web") is None:
            return back(err="Es läuft bereits ein Job – bitte warten, bis er fertig ist.")
        return back(msg=f"{label} gestartet.")

    @app.post("/actions/new-video")
    def new_video(count: int = Form(1)):
        count = max(1, min(count, 10))
        return start("pipeline", ["pipeline", "--channel", channel.key, "--limit", str(count)],
                     f"Erstellung von {count} Video(s)")

    @app.post("/actions/upload")
    def upload(when: str = Form("termin"), at: str = Form("")):
        args = ["upload", "--channel", channel.key]
        if when == "sofort":
            args.append("--sofort")
        elif when == "wunsch":
            try:
                parse_publish_at(at.replace("T", " "), datetime.now(timezone.utc))
            except ValueError as exc:
                return back(err=str(exc))
            args += ["--publish-at", at.replace("T", " ")]
        if when in ("sofort", "wunsch"):
            args += ["--mode", "api"]
        return start("upload", args, "Upload")

    @app.post("/actions/upload-one")
    def upload_one(topic_id: int = Form(...), slot: str = Form("next")):
        args = ["upload", "--channel", channel.key, "--only", str(topic_id)]
        if slot == "sofort":
            args += ["--sofort", "--mode", "api"]
        elif slot != "next":  # konkreter Termin aus dem Dropdown (UTC-ISO)
            local = datetime.fromisoformat(slot.replace("Z", "+00:00")).astimezone(BERLIN)
            args += ["--publish-at", local.strftime("%Y-%m-%d %H:%M"), "--mode", "api"]
        return start("upload", args, f"Upload von #{topic_id}")

    @app.post("/topics/{topic_id}/meta")
    def topic_meta(topic_id: int, title: str = Form(...), description: str = Form(""), hashtags: str = Form("")):
        try:
            actions.update_metadata(db(), topic_id, title, description, hashtags)
        except Exception as exc:
            return back(err=f"#{topic_id}: {exc}")
        return back(msg=f"#{topic_id}: YouTube-Angaben gespeichert.")

    @app.post("/actions/check-public")
    def check_public():
        return start("check-public", ["check-public", "--channel", channel.key], "Sichtbarkeitsprüfung")

    @app.post("/topics/{topic_id}/{action}")
    def topic_action(topic_id: int, action: str):
        d = db()
        try:
            if action == "approve":
                actions.decide(s, d, topic_id, approve=True)
                return back(msg=f"#{topic_id} freigegeben.")
            if action == "reject":
                actions.decide(s, d, topic_id, approve=False)
                return back(msg=f"#{topic_id} abgelehnt.")
            if action == "unapprove":
                actions.unapprove(d, topic_id)
                return back(msg=f"#{topic_id}: Freigabe zurückgenommen.")
            if action == "regenerate":
                actions.regenerate(s, d, topic_id)
                return back(msg=f"#{topic_id} wird beim nächsten Lauf neu erzeugt.")
            if action == "retry":
                t = d.get(topic_id)
                back_to = {"neu": Status.NEU, "skript": Status.SKRIPT, "video": Status.SKRIPT,
                           "freigegeben": Status.FREIGEGEBEN}.get(t.prev_status or "neu", Status.NEU)
                d.transition(topic_id, back_to, note=None)
                return back(msg=f"#{topic_id} → {back_to}.")
        except Exception as exc:
            return back(err=f"#{topic_id}: {exc}")
        raise HTTPException(404)

    # ------------------------------------------------------------------ Zeitplan
    def settings_page(request: Request, sched: dict, msg: str = "", err: str = ""):
        d, now = db(), datetime.now(timezone.utc)
        try:
            preview = upcoming_free_slots(sched["publish_slots"], d.taken_publish_slots(channel.key), now, 5)
        except Exception:
            preview = []
        per_week = len(sched["pipeline"]["days"]) * int(sched["pipeline"]["limit"] or 0)
        slots_week = len(sched["publish_slots"])
        balance = ""
        if sched["pipeline"]["enabled"] and per_week > slots_week:
            balance = (f"Es entstehen bis zu {per_week} Videos pro Woche, aber es gibt nur "
                       f"{plural_de(slots_week, 'Termin', 'Termine')} – "
                       "freigegebene Videos stauen sich. Mehr Termine anlegen oder weniger Videos pro Lauf.")
        elif sched["pipeline"]["enabled"] and per_week < slots_week:
            balance = (f"Nur bis zu {plural_de(per_week, 'Video', 'Videos')} pro Woche für "
                       f"{plural_de(slots_week, 'Termin', 'Termine')} – einige Termine bleiben leer.")
        rows = [(slot.split()[0].lower()[:3], slot.split()[1]) for slot in sched["publish_slots"]]
        rows += [("", "")] * 3  # leere Zeilen zum Hinzufügen
        return page(request, "settings.html", msg=msg, err=err, sched=sched, days=DAYS, day_labels=DAY_LABELS,
                    next_runs=scheduler.next_runs(), preview=preview, balance=balance, slot_rows=rows)

    @app.get("/settings")
    def settings_form(request: Request, msg: str = "", err: str = ""):
        return settings_page(request, load_schedule(db(), channel), msg, err)

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        d = db()
        sched = load_schedule(d, channel)
        sched["pipeline"] = {
            "enabled": form.get("pipeline_enabled") == "on",
            "days": [day for day in DAYS if form.get(f"day_{day}") == "on"],
            "time": str(form.get("pipeline_time", "")),
            "limit": int(form.get("pipeline_limit") or 0),
        }
        sched["upload"] = {"enabled": form.get("upload_enabled") == "on", "time": str(form.get("upload_time", ""))}
        slots = []
        for i in range(50):  # Zeilen "Wochentag + Uhrzeit"; leere Zeilen = entfernt
            day, time_ = form.get(f"slot_day_{i}"), str(form.get(f"slot_time_{i}") or "").strip()
            if day is None and not time_:
                continue
            if day and time_:
                slots.append(f"{day.capitalize()} {time_}")
        sched["publish_slots"] = sorted(set(slots), key=lambda x: (DAYS.index(x[:3].lower()), x[4:]))
        problems = save_schedule(d, channel, sched)
        if problems:
            return settings_page(request, sched, err=" ".join(problems))
        scheduler.apply()
        return back(msg="Zeitplan gespeichert.", to="/settings")

    @app.post("/settings/password")
    def change_password(request: Request, old: str = Form(...), new: str = Form(...), new2: str = Form(...)):
        d = db()
        if not auth.verify_password(old, d.get_setting("password_hash", "")):
            return back(err="Altes Passwort falsch.", to="/settings")
        if new != new2:
            return back(err="Neue Passwörter stimmen nicht überein.", to="/settings")
        try:
            auth.set_password(d, new)
        except ValueError as exc:
            return back(err=str(exc), to="/settings")
        return login_response(d)

    # ------------------------------------------------------------------ Logs
    @app.get("/jobs/{job_id}")
    def job_log(request: Request, job_id: int):
        job = db().get_job(job_id)
        if not job:
            raise HTTPException(404)
        from markupsafe import Markup, escape
        log_html = Markup(linkify_topics(str(escape(tail(job["log_path"], 400)))))
        return page(request, "job.html", job=job, log_html=log_html)

    return app


def run_web(settings: Settings, host: str = "0.0.0.0", port: int = 8080) -> None:
    import uvicorn

    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")
