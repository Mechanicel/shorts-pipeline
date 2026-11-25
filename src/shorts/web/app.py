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
from ..config import ChannelConfig, Settings, load_channels, load_settings
from ..costs import format_cost
from ..analytics import latest_stats, style_comparison
from ..deadline import choose_slot, deadline_of
from ..render import probe_duration
from ..db import Database, Status
from ..script import word_count
from ..review_bot import source_date_label
from ..upload import (BERLIN, STUDIO_URL, parse_publish_at, quota_reset,
                      upcoming_free_slots, upload_status)
from . import auth
from .jobs import JobRunner, args_label, tail
from .schedule import DAY_LABELS, DAYS, Scheduler, load_schedule, save_schedule
from .views import (STYLE_COLORS, STYLE_LONG, STYLE_SHORT, TABS, fmt_dt, fmt_rel, humanize_error, needs_attention,
                    parse_iso, parse_rate, scatter_svg, style_key, tab_counts, timeline, video_rows, video_state,
                    views_last_days)
from markupsafe import Markup

JOBS_PER_PAGE = 25

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
PUBLIC_PATHS = {"/login", "/setup", "/healthz"}
CHANNEL_COOKIE = "kanal"
STYLE_CHOICES = {"auto": "Abwechselnd", "v2": "v2 · Hook-Tafel", "v3": "v3 · Merkliste"}


def local_time(iso: str | None) -> str:
    return fmt_dt(iso)


def job_label(args_json: str) -> str:
    """CLI-Argumente → lesbarer Name ('Upload · sofort · #623')."""
    a = json.loads(args_json)
    kind = a[0] if a else "?"
    if kind == "pipeline":
        n = a[a.index("--limit") + 1] if "--limit" in a else "?"
        style = a[a.index("--style") + 1] if "--style" in a else "auto"
        return f"Videos erstellen ({n})" + (f" · {style}" if style in ("v2", "v3") else "")
    if kind == "upload":
        how = "sofort" if "--sofort" in a else ("Wunschtermin" if "--publish-at" in a else "nächste Termine")
        only = f" · #{a[a.index('--only') + 1]}" if "--only" in a else ""
        return f"Upload · {how}{only}"
    if kind == "research":
        if "--analyze-reviews" in a:
            return "Recherche · Muster auswerten"
        n = a[a.index("--terms") + 1] if "--terms" in a else "alle"
        return f"Recherche ({n} Suchbegriffe)"
    return {"check-public": "Sichtbarkeit prüfen"}.get(kind, " ".join(a))


def duration(job: dict) -> str:
    if not job.get("finished_at"):
        return "läuft"
    secs = int((datetime.fromisoformat(job["finished_at"]) - datetime.fromisoformat(job["started_at"])).total_seconds())
    return f"{secs // 60}:{secs % 60:02d} min"


def linkify_topics(text: str) -> str:
    """'#123' im (bereits escapten) Log → Link zur Karte in der Übersicht."""
    return re.sub(r"#(\d{1,6})\b", r'<a href="/videos/\1">#\1</a>', text)


_PLAN_WORDS = re.compile(r"\b(Gesetzentwurf|Referentenentwurf|Entwurf|Eckpunkte|geplant|plant|soll(en)? künftig)\b", re.I)
_HEDGE_WORDS = re.compile(r"\b(soll|sollen|geplant|plant|Entwurf|voraussichtlich|könnte|kann)\b", re.I)


def warning_badges(topic, today=None) -> list[str]:
    """Auffällige Hinweise oben auf der Freigabe-Karte (Faktencheck-Hilfe)."""
    from datetime import date
    badges = []
    if dl := deadline_of(topic.script, today or date.today()):
        badges.append(f"{dl.kind} {dl.day:%d.%m.%Y} – spätestens {dl.latest_publish_day:%d.%m.} veröffentlichen")
    if topic.note:
        if "Zitate" in topic.note:
            badges.append("Zitat nicht wörtlich belegt")
        if "Grafik-Zahlen" in topic.note:
            badges.append("Zahl nicht im Quelltext")
        if "Ähnliches Thema" in topic.note:
            badges.append("Mögliche Dublette")
    if topic.published_at:
        age = ((today or date.today()) - datetime.fromisoformat(topic.published_at[:10]).date()).days
        if age > 7:
            badges.append(f"Quelle {age} Tage alt")
    else:
        badges.append("Datum unbekannt")
    script = topic.script or {}
    narration = script.get("narration", "")
    if script.get("style") == "v3-merkliste":
        from ..merkliste import status_warnings
        if status_warnings(script.get("items", []), script.get("headline", "")):
            badges.append("Status nicht eindeutig belegt")
    elif topic.source_text and _PLAN_WORDS.search(topic.source_text) and not _HEDGE_WORDS.search(narration):
        badges.append("Rechtsstatus prüfen: Quelle spricht von Plan/Entwurf")
    return badges


def plural_de(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


TEMPLATES.env.filters["local_time"] = local_time
TEMPLATES.env.filters["dt"] = fmt_dt
TEMPLATES.env.filters["day"] = lambda iso: fmt_dt(iso, False)
TEMPLATES.env.filters["rel"] = fmt_rel
TEMPLATES.env.filters["args_label"] = args_label
TEMPLATES.env.filters["job_label"] = job_label
TEMPLATES.env.globals.update(duration=duration, warning_badges=warning_badges, plural=plural_de)


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    s = settings or load_settings()
    channels = load_channels(s.channels_file)
    default_key = s.default_channel if s.default_channel in channels else next(iter(channels))

    def db() -> Database:  # eine Verbindung pro Request/Thread (sqlite3 ist nicht threadsicher)
        return Database(s.db_path)

    def current(request: Request) -> ChannelConfig:
        """Gewählter Kanal (Cookie), sonst DEFAULT_CHANNEL."""
        return channels.get(request.cookies.get(CHANNEL_COOKIE, ""), channels[default_key])

    runner = JobRunner(s, db)
    schedulers = {key: Scheduler(db, ch, runner.launch) for key, ch in channels.items()}  # Zeitplan je Kanal
    throttle = auth.LoginThrottle()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            for sch in schedulers.values():
                sch.start()
        yield
        if start_scheduler:
            for sch in schedulers.values():
                sch.shutdown()

    app = FastAPI(title="Shorts-Pipeline", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runner, app.state.schedulers = runner, schedulers

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
        text = {404: "Seite oder Eintrag nicht gefunden", 405: "Diese Aktion geht so nicht"}.get(
            exc.status_code, f"Fehler {exc.status_code}")
        return TEMPLATES.TemplateResponse(request, "error.html", {"channel": current(request), "channels": channels, "running": None, "nav": {},
                                          "path": request.url.path, "code": exc.status_code, "text": text},
                                          status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        log.exception("Fehler bei %s", request.url.path)
        return TEMPLATES.TemplateResponse(request, "error.html", {"channel": current(request), "channels": channels, "running": None, "nav": {},
                                          "path": request.url.path, "code": 500,
                                          "text": "Interner Fehler, Details stehen im Log des Web-Dienstes"},
                                          status_code=500)

    def waiting_count(d: Database, key: str) -> int:
        return len(d.by_status(Status.VIDEO, key)) + len(d.by_status(Status.WARTET, key))

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        d = db()
        channel = current(request)
        running = runner.running()
        nav = {"freigabe": waiting_count(d, channel.key),
               "jobs": "läuft" if running else d.job_problems_since(
                   (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds"))}
        # Andere Kanäle mit wartenden Videos: Hinweis im Kanal-Umschalter, damit nichts liegen bleibt
        others = {k: waiting_count(d, k) for k in channels if k != channel.key} if len(channels) > 1 else {}
        return TEMPLATES.TemplateResponse(request, name, {"channel": channel, "channels": channels,
                                                          "others_waiting": others, "running": running, "nav": nav,
                                                          "path": request.url.path, **ctx})

    def safe_next(target: str | None, default: str) -> str:
        """Nur interne Pfade als Rücksprung (kein offener Redirect)."""
        return target if target and target.startswith("/") and not target.startswith("//") else default

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

    # ------------------------------------------------------------------ Gemeinsame Daten
    def approval_context(d: Database, channel: ChannelConfig, now: datetime) -> dict:
        sched = load_schedule(d, channel)
        free_slots = upcoming_free_slots(sched["publish_slots"], d.taken_publish_slots(channel.key), now, 6)
        approved = d.by_status(Status.FREIGEGEBEN, channel.key)
        many_free = upcoming_free_slots(sched["publish_slots"], d.taken_publish_slots(channel.key), now, 40)
        slot_hints = {t.id: choose_slot(many_free, deadline_of(t.script, now.date()), now) for t in approved}
        waiting = d.by_status(Status.VIDEO, channel.key) + d.by_status(Status.WARTET, channel.key)
        return {"sched": sched, "free_slots": free_slots, "approved": approved, "slot_hints": slot_hints,
                "waiting": waiting}

    # ------------------------------------------------------------------ Heute
    @app.get("/")
    def heute(request: Request, msg: str = "", err: str = ""):
        from ..costs import total as cost_total
        from ..deadline import planned_stock, stock_warning

        d = db()
        channel = current(request)
        now = datetime.now(timezone.utc)
        ctx = approval_context(d, channel, now)
        stock = planned_stock(d.planned_publish_times(channel.key), now)
        month_start = (now - timedelta(days=30)).isoformat()
        videos_30 = [t for t in d.recent_topics([Status.VIDEO, Status.WARTET, Status.FREIGEGEBEN, Status.PAKET,
                                                  Status.HOCHGELADEN], 300, channel.key)
                     if t.cost_json and t.updated_at >= month_start]
        avg_video = (sum(cost_total(t.cost_json) for t in videos_30) / len(videos_30)) if videos_30 else 0.0
        deadline_warnings = [(t, h.warning) for t in ctx["approved"] if (h := ctx["slot_hints"][t.id]).warning]
        for t in ctx["waiting"]:
            if (dl := deadline_of(t.script, now.date())) and dl.latest_publish_day < now.astimezone(BERLIN).date():
                deadline_warnings.append((t, f"{dl.kind} {dl.day:%d.%m.} – zum Veröffentlichen fast zu spät"))
        block = upload_status(s, d, now)["block"]
        attention = needs_attention(d.by_status(Status.FEHLER, channel.key), deadline_warnings,
                                    stock_warning(stock), block["text"] if block else None)
        return page(request, "heute.html", msg=msg, err=err, attention=attention, stock=stock,
                    waiting_n=len(ctx["waiting"]), views7=views_last_days(d, 7, channel=channel.key),
                    cost_month=d.costs_since(month_start, channel.key), style_choices=STYLE_CHOICES, avg_video=avg_video, eur=s.eur_per_usd,
                    rows=timeline(d, channel.key, ctx["sched"]["publish_slots"], now), now=now,
                    in_work=d.by_status(Status.SKRIPT, channel.key))

    # ------------------------------------------------------------------ Freigabe
    @app.get("/freigabe")
    def freigabe(request: Request, msg: str = "", err: str = ""):
        d = db()
        channel = current(request)
        now = datetime.now(timezone.utc)
        ctx = approval_context(d, channel, now)
        uploaded = d.recent_topics([Status.HOCHGELADEN, Status.PAKET], 15, channel.key)
        return page(request, "freigabe.html", msg=msg, err=err, **ctx, uploads=upload_status(s, d, now),
                    api_public=any(t.youtube_id and t.yt_visibility == "öffentlich" for t in uploaded),
                    upload_mode=s.upload_mode, source_date_label=source_date_label, format_cost=format_cost,
                    word_count=word_count, eur=s.eur_per_usd, style_choices=STYLE_CHOICES)

    # ------------------------------------------------------------------ Videos
    @app.get("/videos")
    def videos(request: Request, tab: str = "geplant", msg: str = "", err: str = ""):
        d = db()
        channel = current(request)
        now = datetime.now(timezone.utc)
        tab = tab if tab in TABS else "geplant"
        rows = video_rows(d, channel.key, tab, now, latest_stats(d))
        checked = max((t.yt_checked_at for t in d.by_status(Status.HOCHGELADEN, channel.key) if t.yt_checked_at),
                      default=None)
        return page(request, "videos.html", msg=msg, err=err, rows=rows, tab=tab, tabs=TABS,
                    counts=tab_counts(d, channel.key, now), checked=checked)

    @app.get("/videos/{topic_id}")
    def video_detail(request: Request, topic_id: int, msg: str = "", err: str = ""):
        d = db()
        try:
            t = d.get(topic_id)
        except KeyError:
            raise HTTPException(404)
        now = datetime.now(timezone.utc)
        return page(request, "video.html", msg=msg, err=err, t=t, s_=t.script or {}, state=video_state(t, now),
                    style=STYLE_LONG.get(style_key(t), style_key(t)), stats=latest_stats(d).get(t.id),
                    history=d.visibility_history(t.id), format_cost=format_cost, eur=s.eur_per_usd,
                    studio_url=STUDIO_URL, source_date_label=source_date_label, word_count=word_count,
                    error=humanize_error(t.note) if t.status == Status.FEHLER else None,
                    has_board=bool(t.video_path and (Path(t.video_path).parent / "board.png").exists()),
                    style_choices=STYLE_CHOICES,
                    channel_name=channels[t.channel].name if t.channel in channels else t.channel)

    # ------------------------------------------------------------------ Statistik
    @app.get("/statistik")
    def statistik(request: Request, msg: str = "", err: str = ""):
        d = db()
        channel = current(request)
        now = datetime.now(timezone.utc)
        stats = latest_stats(d)
        public = video_rows(d, channel.key, "oeffentlich", now, stats)
        styles = style_comparison(d, channel.key, probe_duration)
        for row in styles:  # Ø gesehen % und Ø angesehen vs. weggewischt % je Stil
            mine = [r for r in public if r.style == STYLE_SHORT.get(row["style"], row["style"])]
            pct = [r.avg_pct for r in mine if r.avg_pct is not None]
            rate = [float(r.viewed_rate) for r in mine if r.viewed_rate]
            row["avg_pct"] = sum(pct) / len(pct) if pct else None
            row["avg_rate"] = sum(rate) / len(rate) if rate else None
            row["with_rate"] = len(rate)
        points = [(parse_iso(r.topic.yt_published_at or r.topic.publish_at), float(r.viewed_rate), r.style,
                   (r.topic.script or {}).get("title", r.topic.title)) for r in public if r.viewed_rate]
        return page(request, "statistik.html", msg=msg, err=err, styles=styles, style_labels=STYLE_LONG,
                    chart=Markup(scatter_svg([p for p in points if p[0]])), style_colors=STYLE_COLORS,
                    missing=[r for r in public if not r.viewed_rate], n_points=len(points))

    @app.post("/statistik/studio-werte")
    async def studio_werte(request: Request):
        form = await request.form()
        d, saved, bad = db(), 0, []
        for key, value in form.items():
            if not key.startswith("rate_"):
                continue
            tid = int(key[5:])
            try:
                rate = parse_rate(str(value))
            except ValueError:
                bad.append(f"#{tid}")
                continue
            if rate is not None:
                d.update_fields(tid, yt_viewed_rate=f"{rate:g}")
                saved += 1
        if bad:
            return back(err=f"Ungültige Werte bei {', '.join(bad)} – bitte 0 bis 100 eintragen. "
                            f"{saved} gespeichert.", to="/statistik")
        return back(msg=f"{plural_de(saved, 'Wert', 'Werte')} gespeichert.", to="/statistik")

    # ------------------------------------------------------------------ Jobs
    @app.get("/jobs")
    def jobs(request: Request, filter: str = "", seite: int = 1):
        rows, total = db().jobs_page(filter, max(seite, 1), JOBS_PER_PAGE)
        pages = max(1, -(-total // JOBS_PER_PAGE))
        return page(request, "jobs.html", jobs=rows, total=total, flt=filter, seite=max(seite, 1), pages=pages)

    @app.get("/static/lucide.ttf")
    def icon_font():  # Lucide-Icons (ISC) lokal ausgeliefert, kein CDN
        return FileResponse(s.assets_dir / "icons" / "lucide.ttf", media_type="font/ttf",
                            headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/media/{topic_id}.mp4")
    def media(topic_id: int):
        t = db().get(topic_id)
        if not t.video_path:
            raise HTTPException(404)
        path = Path(t.video_path).resolve()
        if not path.is_relative_to(s.output_dir.resolve()) or not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="video/mp4")

    @app.get("/media/{topic_id}/board.png")
    def media_board(topic_id: int):
        t = db().get(topic_id)
        if not t.video_path:
            raise HTTPException(404)
        path = (Path(t.video_path).parent / "board.png").resolve()
        if not path.is_relative_to(s.output_dir.resolve()) or not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    # ------------------------------------------------------------------ Aktionen
    def start(kind: str, args: list[str], label: str, to: str = "/") -> RedirectResponse:
        if runner.launch(kind, args, "web") is None:
            return back(err="Es läuft bereits ein Job. Bitte warten, bis er fertig ist", to=to)
        return back(msg=f"{label} gestartet", to=to)

    @app.post("/kanal")
    def switch_channel(key: str = Form(...), next: str = Form("/")):
        if key not in channels:
            raise HTTPException(404)
        to = safe_next(next, "/")
        if re.match(r"^/(videos|recherche|jobs)/[^/]+$", to.split("?")[0]):  # Detailseite → Liste
            to = to.split("?")[0].rsplit("/", 1)[0]
        r = RedirectResponse(to, status_code=303)
        r.set_cookie(CHANNEL_COOKIE, key, max_age=365 * 86400, httponly=True, samesite="strict")
        return r

    @app.post("/actions/new-video")
    def new_video(request: Request, count: int = Form(1), style: str = Form("auto")):
        channel = current(request)
        count = max(1, min(count, 10))
        args = ["pipeline", "--channel", channel.key, "--limit", str(count)]
        picked = style if style in ("v2", "v3") else None
        if picked:
            args += ["--style", picked]
        label = f"Erstellung von {plural_de(count, 'Video', 'Videos')}" + (f" ({picked})" if picked else "")
        return start("pipeline", args, label)

    @app.post("/actions/upload")
    def upload(request: Request, when: str = Form("termin"), at: str = Form("")):
        args = ["upload", "--channel", current(request).key]
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
        return start("upload", args, "Upload", to="/freigabe")

    @app.post("/actions/upload-one")
    def upload_one(topic_id: int = Form(...), slot: str = Form("next")):
        args = ["upload", "--channel", db().get(topic_id).channel, "--only", str(topic_id)]
        if slot == "sofort":
            args += ["--sofort", "--mode", "api"]
        elif slot != "next":  # konkreter Termin aus dem Dropdown (UTC-ISO)
            local = datetime.fromisoformat(slot.replace("Z", "+00:00")).astimezone(BERLIN)
            args += ["--publish-at", local.strftime("%Y-%m-%d %H:%M"), "--mode", "api"]
        return start("upload", args, f"Upload von #{topic_id}", to="/freigabe")

    @app.post("/topics/{topic_id}/viewed-rate")
    def topic_viewed_rate(topic_id: int, rate: str = Form(""), next: str = Form("")):
        to = safe_next(next, f"/videos/{topic_id}")
        try:
            value = parse_rate(rate)
        except ValueError:
            return back(err=f"#{topic_id}: Bitte einen Prozentwert zwischen 0 und 100 eintragen", to=to)
        db().update_fields(topic_id, yt_viewed_rate=f"{value:g}" if value is not None else None)
        return back(msg=f"#{topic_id}: Angesehen vs. weggewischt gespeichert", to=to)

    @app.post("/topics/{topic_id}/meta")
    def topic_meta(topic_id: int, title: str = Form(...), description: str = Form(""), hashtags: str = Form(""),
                   next: str = Form("")):
        to = safe_next(next, "/freigabe")
        try:
            actions.update_metadata(db(), topic_id, title, description, hashtags)
        except Exception as exc:
            return back(err=f"#{topic_id}: {exc}", to=to)
        return back(msg=f"#{topic_id}: YouTube-Angaben gespeichert", to=to)

    @app.post("/actions/check-public")
    def check_public(request: Request):
        return start("check-public", ["check-public", "--channel", current(request).key], "Sichtbarkeitsprüfung")

    @app.post("/topics/{topic_id}/{action}")
    def topic_action(topic_id: int, action: str, next: str = Form(""), style: str = Form("auto")):
        d = db()
        to = safe_next(next, "/freigabe")
        try:
            if action == "approve":
                actions.decide(s, d, topic_id, approve=True)
                return back(msg=f"#{topic_id} freigegeben", to=to)
            if action == "reject":
                actions.decide(s, d, topic_id, approve=False)
                return back(msg=f"#{topic_id} abgelehnt", to=to)
            if action == "unapprove":
                actions.unapprove(d, topic_id)
                return back(msg=f"#{topic_id}: Freigabe zurückgenommen", to=to)
            if action == "regenerate":
                pick = style if style in ("v2", "v3") else None
                actions.regenerate(s, d, topic_id, style_pick=pick)
                return back(msg=f"#{topic_id} wird beim nächsten Lauf neu erzeugt" + (f" (als {pick})" if pick else ""),
                            to=to)
            if action == "retry":
                t = d.get(topic_id)
                back_to = {"neu": Status.NEU, "skript": Status.SKRIPT, "video": Status.SKRIPT,
                           "freigegeben": Status.FREIGEGEBEN}.get(t.prev_status or "neu", Status.NEU)
                d.transition(topic_id, back_to, note=None)
                return back(msg=f"#{topic_id} wird erneut versucht", to=to)
        except Exception as exc:
            return back(err=f"#{topic_id}: {exc}", to=to)
        raise HTTPException(404)

    # ------------------------------------------------------------------ Zeitplan
    def settings_page(request: Request, sched: dict, msg: str = "", err: str = ""):
        d, now = db(), datetime.now(timezone.utc)
        channel = current(request)
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
        return page(request, "einstellungen.html", msg=msg, err=err, sched=sched, days=DAYS, day_labels=DAY_LABELS,
                    next_runs=schedulers[channel.key].next_runs(), preview=preview, balance=balance, slot_rows=rows,
                    reset_time=f"{quota_reset(now):%H:%M}")

    # ---------------------------------------------------------------- Ausreißer-Recherche
    @app.get("/recherche")
    def recherche(request: Request, sort: str = "score", msg: str = "", err: str = ""):
        from ..research import MIN_REVIEWS_FOR_ANALYSIS, REVIEW_FIELDS, ResearchStore, short_url

        d = db()
        store = ResearchStore(d)
        rows = store.outliers(order=sort)
        return page(request, "recherche.html", rows=[r for r in rows if r.get("relevant") != "nein"],
                    excluded=[r for r in rows if r.get("relevant") == "nein"], sort=sort, fields=REVIEW_FIELDS,
                    reviewed=store.review_count(), min_reviews=MIN_REVIEWS_FOR_ANALYSIS, last_run=store.last_run(),
                    analysis=d.get_setting("research_review_analysis"), short_url=short_url,
                    has_key=bool(s.yt_api_key), msg=msg, err=err)

    @app.get("/recherche/{video_id}")
    def recherche_item(request: Request, video_id: str, msg: str = "", err: str = ""):
        from ..research import REVIEW_FIELDS, ResearchStore, short_url

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", video_id):
            raise HTTPException(404)
        row = ResearchStore(db()).get_outlier(video_id)
        if not row:
            raise HTTPException(404)
        return page(request, "recherche_item.html", r=row, fields=REVIEW_FIELDS, short_url=short_url,
                    tags=json.loads(row.get("tags") or "[]"), msg=msg, err=err)

    @app.post("/recherche/{video_id}/sichtung")
    @app.post("/research/{video_id}/review")
    async def research_review(video_id: str, request: Request):
        from ..research import ResearchStore

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", video_id):
            raise HTTPException(404)
        form = await request.form()
        ResearchStore(db()).save_review(video_id, {k: str(v) for k, v in form.items()})
        return back(msg="Sichtung gespeichert", to="/recherche")

    @app.post("/actions/research")
    def research_run(request: Request, terms: int = Form(10)):
        terms = max(1, min(terms, 40))
        return start("research", ["research", "--channel", current(request).key, "--terms", str(terms)],
                     f"Recherche mit {terms} Suchbegriffen", to="/recherche")

    @app.post("/actions/research-patterns")
    def research_patterns(request: Request):
        return start("research", ["research", "--channel", current(request).key, "--analyze-reviews"],
                     "Muster-Auswertung", to="/recherche")

    @app.get("/einstellungen")
    def einstellungen(request: Request, msg: str = "", err: str = ""):
        return settings_page(request, load_schedule(db(), current(request)), msg, err)

    # Alte Adressen (Lesezeichen, Telegram-Links) leiten weiter
    @app.get("/settings")
    def old_settings(request: Request):
        return RedirectResponse("/einstellungen" + (f"?{request.url.query}" if request.url.query else ""), 307)

    @app.get("/research")
    def old_research(request: Request):
        return RedirectResponse("/recherche", status_code=307)

    @app.post("/einstellungen")
    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        d = db()
        channel = current(request)
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
        schedulers[channel.key].apply()
        return back(msg="Zeitplan gespeichert", to="/einstellungen")

    @app.post("/settings/password")
    def change_password(request: Request, old: str = Form(...), new: str = Form(...), new2: str = Form(...)):
        d = db()
        if not auth.verify_password(old, d.get_setting("password_hash", "")):
            return back(err="Altes Passwort falsch", to="/einstellungen")
        if new != new2:
            return back(err="Neue Passwörter stimmen nicht überein", to="/einstellungen")
        try:
            auth.set_password(d, new)
        except ValueError as exc:
            return back(err=str(exc), to="/einstellungen")
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
