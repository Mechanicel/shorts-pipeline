"""FastAPI-App: Übersicht, Freigabe, Upload, Zeitplan, Job-Logs. Nur fürs Heimnetz gedacht."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import actions
from ..config import Settings, get_channel, load_settings
from ..costs import format_cost
from ..db import Database, Status
from ..script import word_count
from ..review_bot import source_date_label
from ..upload import BERLIN, STUDIO_URL, UPLOAD_COST, WOCHENTAGE, parse_publish_at, quota_status
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


TEMPLATES.env.filters["local_time"] = local_time
TEMPLATES.env.filters["args_label"] = args_label


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
        # Ob API-Uploads öffentlich werden, aus echten Prüfergebnissen ableiten (nicht behaupten)
        api_public = any(t.youtube_id and t.yt_visibility == "öffentlich" for t in uploaded)
        upload_blocked = s.upload_mode == "api" and quota["possible"] <= 0
        return page(
            request, "dashboard.html", msg=msg, err=err,
            waiting=d.by_status(Status.WARTET, channel.key),
            approved=d.by_status(Status.FREIGEGEBEN, channel.key),
            uploaded=uploaded, quota=quota, api_public=api_public, upload_blocked=upload_blocked,
            upload_cost=UPLOAD_COST, source_date_label=source_date_label,
            failed=d.by_status(Status.FEHLER, channel.key),
            in_work=d.by_status(Status.SKRIPT, channel.key) + d.by_status(Status.VIDEO, channel.key),
            counts=d.counts(channel.key), jobs=d.recent_jobs(10),
            cost_week=d.costs_since((now - timedelta(days=7)).isoformat()),
            cost_month=d.costs_since((now - timedelta(days=30)).isoformat()),
            eur=s.eur_per_usd, sched=sched, next_runs=scheduler.next_runs(),
            upload_mode=s.upload_mode, format_cost=format_cost, word_count=word_count,
            studio_url=STUDIO_URL, day_labels=DAY_LABELS,
            default_at=(now + timedelta(minutes=30)).astimezone(BERLIN).strftime("%Y-%m-%dT%H:%M"),
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
    @app.get("/settings")
    def settings_form(request: Request, msg: str = "", err: str = ""):
        return page(request, "settings.html", msg=msg, err=err, sched=load_schedule(db(), channel),
                    days=DAYS, day_labels=DAY_LABELS, next_runs=scheduler.next_runs())

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
        sched["publish_slots"] = [line.strip() for line in str(form.get("publish_slots", "")).splitlines()
                                  if line.strip()]
        problems = save_schedule(d, channel, sched)
        if problems:
            return page(request, "settings.html", err=" ".join(problems), sched=sched, days=DAYS,
                        day_labels=DAY_LABELS, next_runs=scheduler.next_runs())
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
        return page(request, "job.html", job=job, log_text=tail(job["log_path"], 400))

    return app


def run_web(settings: Settings, host: str = "0.0.0.0", port: int = 8080) -> None:
    import uvicorn

    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")
