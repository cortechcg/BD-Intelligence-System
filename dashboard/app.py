"""FastAPI web service: the dashboard UI and a thin API.

Stateless. Every piece of real state lives in Supabase, so this process can be
restarted or scaled without losing anything — the same guarantee Phase 7 gave
the core pipeline.

It can queue pipeline runs (``dashboard_triggers``) and ask a running one to
stop. It cannot send email, submit a proposal, or advance anything past
``drafted``. There is no route that does, by construction.

    uvicorn dashboard.app:app --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger

from config import CLAUDE_MODEL
from dashboard import auth, queries, settings, triggers
from database.supabase_client import (
    save_accepted_outline,
    seed_auto_pipeline,
    seed_direct_draft,
)
from intelligence.draft_templates import catalog, resolve_outline
from processors.downloader import extract_uploaded_files
from utils.urls import canonicalize_url

HERE = Path(__file__).resolve().parent

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Make the running service's identity unmistakable in the first log lines.

    After the 2026-09-21 incident (a Render web service booted bare main.py and
    ran the discovery scheduler), every long-lived process states what it is
    and what it will never do, before it does anything else.
    """
    logger.info(
        "IDENTITY: cortech-bd-web — dashboard web service (FastAPI). Serves UI + "
        "API and writes dashboard_triggers only. This process never runs "
        "discovery, extraction, scoring or drafting and has no scheduler."
    )
    yield


class _FreshStatic(StaticFiles):
    """CSS and JS must revalidate. A same-URL stylesheet was staying cream in
    the browser after the abyssal theme shipped."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path.endswith((".css", ".js")):
            response.headers["Cache-Control"] = "no-cache"
        return response


def _static_version() -> str:
    assets = [
        HERE / "static" / "app.css",
        HERE / "static" / "redesign.css",
        HERE / "static" / "track.css",
        HERE / "static" / "track.js",
        HERE / "static" / "glass.css",
        HERE / "static" / "type.css",
        HERE / "static" / "motion.js",
        HERE / "static" / "library.css",
        HERE / "static" / "library.js",
        HERE / "static" / "pending.css",
        HERE / "static" / "pending.js",
    ]
    try:
        return format(int(max(asset.stat().st_mtime for asset in assets)), "x")
    except OSError:
        return "0"


app = FastAPI(
    title=f"{settings.PRODUCT_NAME} control", docs_url=None, redoc_url=None, openapi_url=None,
    lifespan=_lifespan,
)
app.mount("/static", _FreshStatic(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


@app.middleware("http")
async def _dont_cache_pages(request: Request, call_next):
    """Pages must not stick in the browser. A cached shell keeps the old desk
    after a deploy, because the poll only replaces <main>."""
    response = await call_next(request)
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-store"
    return response


PUBLIC_PATHS = {"/auth/login", "/auth/google", "/auth/callback", "/auth/denied", "/healthz"}

#: The one description of what Cancel does. Rendered verbatim in the UI and
#: asserted by tests/test_dashboard_controls.py against the code path, so the
#: copy cannot drift from the behaviour.
CANCEL_COPY = (
    "Cancel is cooperative, not instant. A job that has not started is "
    "withdrawn immediately and the worker never sees it. A job that is running "
    "is flagged; the worker's next heartbeat (every "
    f"{int(settings.HEARTBEAT_SECONDS)}s) asks the pipeline to halt before its "
    "next model call. The model call already in flight finishes, up to "
    "ANTHROPIC_TIMEOUT_SECONDS, and its cost is incurred and recorded. The "
    "opportunity keeps its last saved stage and checkpoint and can be resumed. "
    "Cancelled spend still counts toward the aggregate cap."
)


# ── security headers ────────────────────────────────────────────────────────

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Tender titles and model prose are rendered on these pages. Jinja
    # autoescapes them; this is the second layer. Inline style is used only
    # for chart-bar widths; inline script for the poll/notify snippet.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; font-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'",
    )
    return response


# ── session ─────────────────────────────────────────────────────────────────

def current_session(request: Request) -> dict | None:
    return auth.read_session(request.cookies.get(settings.SESSION_COOKIE_NAME))


def require_session(request: Request) -> dict:
    session = current_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="sign in required")
    return session


@app.middleware("http")
async def enforce_auth(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if current_session(request) is None:
        if path.startswith("/api/"):
            return JSONResponse({"error": "sign in required"}, status_code=401)
        return RedirectResponse("/auth/login", status_code=302)
    return await call_next(request)


def _base_context(request: Request, view: str) -> dict:
    session = current_session(request)
    return {
        "request": request,
        "view": view,
        "viewer": (session or {}).get("email", ""),
        "csrf_token": auth.csrf_token(session),
        "poll_ms": settings.POLL_MS,
        "static_base": "/static",
        "static_v": _static_version(),
        "url_queue": "/",
        "url_portfolio": "/portfolio",
        "url_library": "/library",
        "url_track_record": "/track-record",
        "url_detail": "/opportunity",
        "url_job": "/job",
        "url_logout": "/auth/logout",
        "url_trigger_url": "/trigger/url",
        "url_trigger_existing": "/trigger/existing",
        "url_trigger_bulk": "/trigger/bulk",
        "url_trigger_retry": "/trigger/retry",
        "url_trigger_rerun": "/trigger/rerun",
        "url_trigger_cancel": "/trigger/cancel",
        "url_draft": "/draft",
        "url_mark_reviewed": "/opportunity/reviewed",
        "url_mark_outcome": "/opportunity/outcome",
        "url_final_grade": "/opportunity/final-grade",
        "url_outline": "/opportunity/outline",
        "cancel_copy": CANCEL_COPY,
        "claude_model": CLAUDE_MODEL,
        "product_name": settings.PRODUCT_NAME,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "flash": request.query_params.get("msg", ""),
        "flash_kind": request.query_params.get("kind", ""),
    }


# ── auth routes ─────────────────────────────────────────────────────────────

def _auth_context(request: Request) -> dict:
    return {
        "request": request,
        "product_name": settings.PRODUCT_NAME,
        "static_base": "/static",
        "static_v": _static_version(),
        "domain": settings.AUTH_ALLOWED_DOMAIN,
        "has_exceptions": bool(settings._allowed_emails()),
        "url_start": "/auth/google",
        "url_login": "/auth/login",
    }


def _denied(request: Request, title: str, reason: str, status: int, hint: str = ""):
    ctx = {**_auth_context(request), "title": title, "reason": reason, "hint": hint}
    return templates.TemplateResponse(request, "denied.html", ctx, status_code=status)


@app.get("/auth/login")
def login(request: Request):
    """The sign-in screen. Presentational only: the OAuth flow starts at
    /auth/google, exactly as before; this page just puts a real screen in
    front of the redirect instead of bouncing straight to Google."""
    if current_session(request):
        return RedirectResponse("/", status_code=302)
    ok, reason = settings.auth_configured()
    ctx = {**_auth_context(request), "configured": ok, "reason": reason}
    return templates.TemplateResponse(request, "login.html", ctx, status_code=200 if ok else 503)


@app.get("/auth/google")
def login_start(request: Request):
    if current_session(request):
        return RedirectResponse("/", status_code=302)
    try:
        url, state_cookie = auth.begin_login()
    except auth.AuthError as exc:
        ctx = {**_auth_context(request), "configured": False, "reason": str(exc)}
        return templates.TemplateResponse(request, "login.html", ctx, status_code=503)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        auth.STATE_COOKIE, state_cookie, max_age=600, httponly=True,
        secure=settings.COOKIE_SECURE, samesite="lax", path="/auth",
    )
    return response


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return _denied(request, "Sign-in did not complete", f"Google returned an error: {error}", 400)
    try:
        claims = auth.complete_login(code, state, request.cookies.get(auth.STATE_COOKIE))
    except auth.AuthError as exc:
        # 403 and the specific reason: a login that fails opaquely gets
        # worked around rather than fixed.
        return _denied(request, "Access denied", str(exc), 403)

    token = auth.issue_session(claims["email"], claims.get("name", ""))
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        settings.SESSION_COOKIE_NAME, token, max_age=settings.SESSION_MAX_AGE_SECONDS,
        httponly=True, secure=settings.COOKIE_SECURE, samesite="lax", path="/",
    )
    response.delete_cookie(auth.STATE_COOKIE, path="/auth")
    return response


@app.get("/auth/logout")
def logout():
    response = RedirectResponse("/auth/login", status_code=302)
    response.delete_cookie(settings.SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/healthz")
def healthz():
    """Liveness only. Deliberately does not touch Supabase: a health check
    that fails on a transient PostgREST blip makes the platform restart a
    perfectly good web process."""
    return PlainTextResponse("ok")


# ── views ───────────────────────────────────────────────────────────────────

def _triggering_state() -> tuple[bool, str]:
    if not settings.TRIGGERS_ENABLED:
        return False, "DASHBOARD_TRIGGERS_ENABLED=false"
    if not triggers.queue_available():
        return False, (
            "the dashboard_triggers table is missing or out of date: apply "
            "supabase_migration_dashboard_triggers.sql and "
            "supabase_migration_dashboard_controls.sql"
        )
    return True, ""


def _spend_context() -> dict:
    window = triggers.spend_window()
    blocked, reason = triggers.aggregate_cap_reached(window)
    return {"spend": window, "cap_blocked": blocked, "cap_reason": reason}


def _sidebar_opps(queue_data: dict | None = None, limit: int = 12) -> list[dict]:
    """Return active opportunity cards for the 260px left rail. Fail-open."""
    try:
        if isinstance(queue_data, dict):
            combined = (
                list(queue_data.get("choose_outline") or [])
                + list(queue_data.get("in_flight") or [])
                + list(queue_data.get("drafted") or [])
                + list(queue_data.get("halted") or [])
                + list(queue_data.get("cache_only") or [])
            )
            return combined[:limit]
        return queries.list_ledger_items(limit=limit)
    except Exception:
        return []


@app.get("/", response_class=HTMLResponse)
def queue_page(request: Request):
    enabled, reason = _triggering_state()
    ctx = _base_context(request, "queue")
    q = queries.queue_view(limit=40)
    ctx.update({
        "queue": q,
        "sidebar_opps": _sidebar_opps(q),
        "draft_templates": catalog(),
        "triggers": triggers.recent(12),
        "in_flight": triggers.in_flight_by_url(),
        "triggering_enabled": enabled,
        "triggering_disabled_reason": reason,
        "bulk_max": settings.BULK_MAX,
        **_spend_context(),
    })
    return templates.TemplateResponse(request, "queue.html", ctx)


@app.get("/opportunity", response_class=HTMLResponse)
def opportunity_page(request: Request, url: str = ""):
    if not url:
        return RedirectResponse("/", status_code=302)
    detail = queries.opportunity_detail(url)
    if detail is None:
        raise HTTPException(status_code=404, detail="no stored row for that URL")

    client_name = detail["client"]["value"] or detail["donor"]["value"]
    enabled, reason = _triggering_state()
    jobs = triggers.for_url(detail["source_url"], limit=5)
    active = next((j for j in jobs if j["status"] in ("queued", "running")), None)
    ctx = _base_context(request, "queue")
    ctx.update({
        "d": detail,
        "sidebar_opps": _sidebar_opps(),
        "org": queries.organization_history(client_name),
        "jobs": jobs,
        "active_job": active,
        # Only poll on the opportunity workspace while a job for this URL is in flight;
        # never overwrite the DOM while a person is customizing the template outline.
        "poll_ms": 5000 if active or detail.get("final_grade_pending") else 0,
        "triggering_enabled": enabled,
        "triggering_disabled_reason": reason,
        **_spend_context(),
    })
    return templates.TemplateResponse(request, "detail.html", ctx)


@app.get("/job/{trigger_id}", response_class=HTMLResponse)
def job_page(request: Request, trigger_id: str):
    job = triggers.get(trigger_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    ledger = queries.opportunity_detail(job["source_url"])
    ctx = _base_context(request, "queue")
    ctx.update({
        "job": job,
        "d": ledger,
        "poll_ms": 5000 if job["status"] in ("queued", "running") else settings.POLL_MS,
        "notify": job["status"] in ("queued", "running"),
    })
    return templates.TemplateResponse(request, "job.html", ctx)


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request):
    portfolio = queries.portfolio_view()
    scored = portfolio["ledger_rows"] - portfolio["unscored_ledger_rows"]
    ctx = _base_context(request, "portfolio")
    ctx.update({
        "p": portfolio,
        "stage_bars": queries.bar_rows(
            [(s, portfolio["by_stage"][s]) for s in queries.ALL_STAGES],
            total=portfolio["ledger_rows"],
            tones=[
                "agent" if s in queries.AGENT_STAGES else "human"
                for s in queries.ALL_STAGES
            ],
        ),
        "rec_bars": queries.bar_rows(
            [(k, portfolio["by_recommendation"].get(k, 0)) for k in ("BID", "WATCH", "NO-BID")],
            total=scored,
            tones=["bid", "watch", "nobid"],
        ),
        "digest": queries.market_digest(),
        "dashboard_runs": triggers.recent(50),
        **_spend_context(),
    })
    ctx["charts"] = _portfolio_charts(portfolio, ctx["digest"], ctx["dashboard_runs"])
    return templates.TemplateResponse(request, "portfolio.html", ctx)


@app.get("/track-record", response_class=HTMLResponse)
def track_record_page(request: Request, q: str = "", client: str = "", year: str = "", area: str = "",
                      theme: str = "", won: str = ""):
    """Read-only track record: the Company Profile 2026 plus the past proposals.

    See dashboard/track_record.py for the rules. It writes nothing; the profile
    renders even when the database is empty or cannot be read. The project
    filters are plain query parameters so the table filters without a script.
    """
    from dashboard import track_record

    t = track_record.load_track_record()
    q, client, year, area, theme = (v.strip()[:120] for v in (q, client, year, area, theme))
    shown = track_record.filter_projects(t["projects"], q=q, client=client, year=year, area=area, theme=theme,
                                         won=won in ("1", "on", "true"))
    ctx = _base_context(request, "track")
    ctx.update({
        "t": t, "poll_ms": 0, "shown": shown, "shown_ns": {p["n"] for p in shown},
        "sel": {"q": q, "client": client, "year": year, "area": area,
                "theme": theme if theme in {x["slug"] for x in t["themes"]} else "",
                "won": won in ("1", "on", "true")},
    })
    return templates.TemplateResponse(request, "track_record.html", ctx)


def _spend_by_day(runs: list[dict]) -> dict:
    """Group dashboard runs by the day they were requested (UTC).

    Two series: spend on runs that finished (`succeeded`) and spend on runs
    that did not (failed, cancelled, cap-halted) — the latter is real money
    too and is never hidden. Runs whose spend is unrecorded are counted in
    `unknown` and drawn as nothing, never as zero.
    """
    days: dict[str, dict] = {}
    for r in runs:
        day = (r.get("requested_at") or "")[:10]
        if not day:
            continue
        d = days.setdefault(day, {"completed": 0.0, "stopped": 0.0, "runs": 0, "unknown": 0})
        d["runs"] += 1
        spend = r.get("spend_usd")
        if spend is None:
            d["unknown"] += 1
            continue
        key = "completed" if r.get("status") == "succeeded" else "stopped"
        d[key] += float(spend)
    labels = sorted(days)
    completed = [round(days[k]["completed"], 4) for k in labels]
    stopped = [round(days[k]["stopped"], 4) for k in labels]
    peak = max((completed[i] + stopped[i] for i in range(len(labels))), default=0.0)
    return {
        "labels": labels,
        "completed": completed,
        "stopped": stopped,
        "runs": [days[k]["runs"] for k in labels],
        "unknown": [days[k]["unknown"] for k in labels],
        "run_count": len(runs),
        "peak": peak,
    }


def _portfolio_charts(portfolio: dict, digest, runs: list[dict]) -> dict:
    """Everything the Portfolio charts draw, as plain JSON. Same numbers the
    page's tables print — the tables stay as the text fallback."""
    def window(w):
        if w is None or not w.is_trend:
            return None
        return {
            "labels": [c.label for c in w.counts[:10]],
            "counts": [c.count for c in w.counts[:10]],
            "coverage": w.coverage_clause(),
            "sample": w.sample_clause(),
            "shown": min(10, len(w.counts)),
            "total_labels": len(w.counts),
        }
    return {
        "stage": {
            "labels": list(queries.ALL_STAGES),
            "counts": [portfolio["by_stage"].get(s, 0) for s in queries.ALL_STAGES],
            "agent_stages": len(queries.AGENT_STAGES),
            "ledger_rows": portfolio["ledger_rows"],
        },
        "spend": _spend_by_day(runs),
        "themes_30": window(digest.themes.get(30)) if digest else None,
        "themes_90": window(digest.themes.get(90)) if digest else None,
        "geography_30": window(digest.geography.get(30)) if digest else None,
    }


@app.get("/draft")
def draft_download(request: Request, url: str = ""):
    """Serve the stored draft from the durable Supabase checkpoint.

    Not from ``output/draft_*.md``: the worker's local filesystem is ephemeral
    on a container host and is not the web service's filesystem anyway.
    """
    sections = queries.draft_sections(url)
    if not sections:
        raise HTTPException(status_code=404, detail="no stored draft for that URL")

    from reporting.docx_builder import iter_client_sections

    skip = {
        "lightweight", "lightweight_reason", "submission_type", "quality_score",
        "claim_grounding", "tender_brief", "win_strategy", "document_lock",
        "section_order", "omitted_financial", "submission_outline",
    }
    parts = [
        "# DRAFT: INTERNAL REVIEW ONLY", "",
        "This document was generated by the Cortech BD agent and has not been",
        "reviewed by a human. Nothing has been sent to the client. Named",
        "past-work claims are grounded or tagged `[NOT VERIFIED]` (ADR 008);",
        "check every one before this leaves the building.", "",
        f"Source: {url}", "", "---", "",
    ]
    written = set()
    for key, heading, content in iter_client_sections(sections):
        if key in skip:
            continue
        parts.append(f"## {heading}\n\n{content}\n")
        written.add(key)
    for name, content in sections.items():
        if name in written or name in skip or not isinstance(content, str) or not content.strip():
            continue
        parts.append(f"## {name.replace('_', ' ').title()}\n\n{content}\n")

    return Response(
        "\n".join(parts),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="cortech-draft.md"'},
    )


# ── triggers ────────────────────────────────────────────────────────────────

def _flash(path: str, msg: str, kind: str = "") -> RedirectResponse:
    q = f"msg={quote(msg)}" + (f"&kind={kind}" if kind else "")
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}{q}", status_code=303)


def _check_csrf(session: dict, token: str) -> None:
    if not auth.csrf_ok(session, token):
        raise HTTPException(status_code=403, detail="stale form: reload and retry")


def _enqueue_one(url: str, kind: str, session: dict, back: str = "/") -> RedirectResponse:
    """Queue one job and land the user on its job page — never a dead end."""
    enabled, reason = _triggering_state()
    if not enabled:
        return _flash(back, f"Triggering is off: {reason}", "stop")

    canonical = canonicalize_url(url) or ""
    if not canonical:
        return _flash(back, "That is not a usable URL.", "stop")

    blocked, why = triggers.aggregate_cap_reached()
    if blocked:
        # Mirror of the worker's gate. The worker enforces it regardless.
        return _flash(back, f"Not queued: {why}", "cap")

    try:
        trigger_id = triggers.enqueue(canonical, kind, session["email"])
    except triggers.QueueUnavailable as exc:
        return _flash(back, str(exc), "stop")
    except ValueError as exc:
        return _flash(back, str(exc), "stop")
    except Exception as exc:
        logger.exception("enqueue failed")
        return _flash(back, f"Could not queue that: {type(exc).__name__}", "stop")

    if trigger_id is None:
        existing = triggers.in_flight_by_url().get(canonical)
        if existing:
            return RedirectResponse(f"/job/{existing['id']}", status_code=303)
        return _flash(back, "Already queued or running. Nothing was started twice.")
    return RedirectResponse(f"/job/{trigger_id}", status_code=303)


def _outline_from_form(form) -> tuple[dict | None, str]:
    """Chapters posted with a link. Empty headings are dropped."""
    headings = [str(value) for value in form.getlist("heading")]
    limits = [str(value) for value in form.getlist("page_limit")]
    musts = [str(value) for value in form.getlist("must_include")]
    sections = []
    for index, heading in enumerate(headings):
        if not heading.strip():
            continue
        raw_must = musts[index] if index < len(musts) else ""
        sections.append({
            "heading": heading,
            "page_limit": limits[index] if index < len(limits) else "",
            "must_include": [
                part.strip()
                for part in raw_must.replace(";", "\n").split("\n")
                if part.strip()
            ],
        })
    payload = {
        "template_id": str(form.get("template_id") or "house_technical"),
        "submission_type": str(form.get("submission_type") or ""),
        "name": str(form.get("outline_name") or ""),
    }
    if sections:
        payload["sections"] = sections
    try:
        return resolve_outline(payload, None), ""
    except ValueError as exc:
        return None, str(exc)


@app.post("/trigger/url")
async def trigger_url(request: Request, session: dict = Depends(require_session)):
    """Score or draft a pasted link or uploaded ToR/RFP file.

    - ``intent=draft``: stores the chosen outline and queues a direct draft.
    - ``intent=auto`` (or uploaded file with ``auto_draft``): seeds the pipeline
      with ``auto_accept_outline=True`` (plus any uploaded file text) so the
      worker runs Analyze -> Score -> Draft in one uninterrupted pass.
    - default (``intent=score`` or empty with URL): queues ``submit_url`` so
      scoring finishes and waits for outline confirmation.
    """
    form = await request.form()
    _check_csrf(session, str(form.get("csrf_token") or ""))
    url = str(form.get("url") or "").strip()
    intent = str(form.get("intent") or "").strip().lower()
    auto_draft = str(form.get("auto_draft") or "").strip().lower() in ("1", "true", "on", "yes")

    uploaded_pairs: list[tuple[str, bytes]] = []
    for item in form.getlist("tor_file"):
        filename = getattr(item, "filename", None)
        read_fn = getattr(item, "read", None)
        if filename and callable(read_fn):
            data = await read_fn()
            if data:
                uploaded_pairs.append((str(filename), bytes(data)))

    extracted_title = ""
    extracted_text = ""
    if uploaded_pairs:
        extracted = extract_uploaded_files(uploaded_pairs)
        if not extracted.get("ok"):
            return _flash("/", str(extracted.get("reason") or "Could not read uploaded file."), "stop")
        extracted_text = str(extracted.get("text") or "")
        extracted_title = str(extracted.get("title") or "")
        if not url:
            url = str(extracted.get("source_url") or "")

    if intent == "draft":
        canonical = canonicalize_url(url) or ""
        if not canonical:
            return _flash("/", "Paste a tender link or upload a ToR/RFP file first.", "stop")
        if triggers.in_flight_by_url().get(canonical):
            return _flash("/", "Already queued or running. Nothing was started twice.", "stop")
        outline, error = _outline_from_form(form)
        if error or outline is None:
            return _flash("/", error or "Choose the chapters first.", "stop")
        if extracted_text:
            ok, reason = seed_auto_pipeline(
                canonical,
                title=extracted_title,
                full_text=extracted_text,
                auto_accept_outline=True,
                accepted_outline=outline,
            )
        else:
            ok, reason = seed_direct_draft(canonical, outline)
        if not ok:
            return _flash("/", reason, "stop")
        return _enqueue_one(canonical, triggers.DRAFT_EXISTING, session)

    if uploaded_pairs or intent == "auto" or auto_draft:
        canonical = canonicalize_url(url) or ""
        if not canonical:
            return _flash("/", "Paste a tender link or upload a ToR/RFP file first.", "stop")
        if triggers.in_flight_by_url().get(canonical):
            return _flash("/", "Already queued or running. Nothing was started twice.", "stop")
        should_auto_draft = True if (intent == "auto" or auto_draft) else False
        ok, reason = seed_auto_pipeline(
            canonical,
            title=extracted_title,
            full_text=extracted_text,
            auto_accept_outline=should_auto_draft,
        )
        if not ok:
            return _flash("/", reason, "stop")
        return _enqueue_one(canonical, triggers.DRAFT_EXISTING, session)

    return _enqueue_one(url, triggers.SUBMIT_URL, session)


@app.post("/trigger/existing")
def trigger_existing(
    request: Request, source_url: str = Form(...), csrf_token: str = Form(""),
    session: dict = Depends(require_session),
):
    _check_csrf(session, csrf_token)
    return _enqueue_one(source_url, triggers.DRAFT_EXISTING, session)


@app.post("/trigger/retry")
def trigger_retry(
    request: Request, source_url: str = Form(...), csrf_token: str = Form(""),
    session: dict = Depends(require_session),
):
    """Retry a dead-lettered opportunity from its checkpoint (not from scratch)."""
    _check_csrf(session, csrf_token)
    back = f"/opportunity?url={quote(source_url, safe='')}"
    return _enqueue_one(source_url, triggers.RETRY_DEAD_LETTER, session, back=back)


@app.post("/trigger/rerun")
def trigger_rerun(
    request: Request, source_url: str = Form(...), from_stage: str = Form(...),
    csrf_token: str = Form(""), session: dict = Depends(require_session),
):
    """Re-run from `extracted` (re-score + re-draft) or `scored` (re-draft)."""
    _check_csrf(session, csrf_token)
    back = f"/opportunity?url={quote(source_url, safe='')}"
    kind = {
        "extracted": triggers.RERUN_FROM_EXTRACTED,
        "scored": triggers.RERUN_FROM_SCORED,
    }.get(from_stage)
    if kind is None:
        return _flash(back, "Re-run is only from `extracted` or `scored`; a full re-run is Submit URL.", "stop")
    return _enqueue_one(source_url, kind, session, back=back)


@app.post("/trigger/bulk")
async def trigger_bulk(request: Request, session: dict = Depends(require_session)):
    """"Draft selected": one job per URL. Fan-out is at the queue, not here."""
    form = await request.form()
    _check_csrf(session, str(form.get("csrf_token") or ""))
    urls = [str(u) for u in form.getlist("source_url") if str(u).strip()]
    if not urls:
        return _flash("/", "Nothing selected.")
    if len(urls) > settings.BULK_MAX:
        return _flash("/", f"Select at most {settings.BULK_MAX} at a time.", "stop")

    enabled, reason = _triggering_state()
    if not enabled:
        return _flash("/", f"Triggering is off: {reason}", "stop")
    blocked, why = triggers.aggregate_cap_reached()
    if blocked:
        return _flash("/", f"Not queued: {why}", "cap")

    try:
        results = triggers.enqueue_many(urls, triggers.DRAFT_EXISTING, session["email"])
    except triggers.QueueUnavailable as exc:
        return _flash("/", str(exc), "stop")
    queued = [r for r in results if r["queued"]]
    skipped = [r for r in results if not r["queued"]]
    msg = f"Queued {len(queued)} job(s)"
    if skipped:
        msg += f"; {len(skipped)} skipped (already in flight)"
    msg += ". Each runs as its own job; the aggregate cap applies to all of them."
    return _flash("/", msg)


@app.post("/trigger/cancel")
def trigger_cancel(
    request: Request, trigger_id: str = Form(...), csrf_token: str = Form(""),
    session: dict = Depends(require_session),
):
    _check_csrf(session, csrf_token)
    outcome = triggers.cancel(trigger_id, session["email"])
    msg = {
        "cancelled_before_start": "Cancelled before it started. The worker will not run it.",
        "halt_requested": (
            "Halt requested. The worker stops before its next model call; the call "
            "in flight finishes and its cost is recorded."
        ),
        "not_found": "No such job.",
    }.get(outcome, f"Nothing to cancel ({outcome.replace('already_', 'job is ')}).")
    return _flash(f"/job/{trigger_id}", msg, "stop" if outcome == "not_found" else "")


def _detail_back(source_url: str) -> str:
    canonical = canonicalize_url(source_url) or source_url
    return canonical, f"/opportunity?url={quote(canonical, safe='')}"


@app.post("/opportunity/reviewed")
def mark_reviewed(
    request: Request,
    source_url: str = Form(...),
    csrf_token: str = Form(""),
    session: dict = Depends(require_session),
):
    """A person marks the draft reviewed. Does not email or submit."""
    _check_csrf(session, csrf_token)
    from database.supabase_client import record_human_pipeline_stage

    canonical, back = _detail_back(source_url)
    if record_human_pipeline_stage(canonical, "reviewed"):
        return _flash(back, "Marked reviewed.")
    return _flash(back, "Could not record reviewed.", "stop")


@app.post("/opportunity/final-grade")
async def grade_final_version(request: Request, session: dict = Depends(require_session)):
    """Grade an uploaded final against stored criteria. Does not email or submit."""
    form = await request.form()
    _check_csrf(session, str(form.get("csrf_token") or ""))
    source_url = str(form.get("source_url") or "").strip()
    _canonical, back = _detail_back(source_url) if source_url else ("", "/")
    if not source_url:
        return _flash("/", "Missing the opportunity.", "stop")
    detail = queries.opportunity_detail(source_url)
    if detail is None:
        return _flash("/", "No stored row for that opportunity.", "stop")
    if not detail.get("has_draft"):
        return _flash(back, "Grade a final after a draft exists.", "stop")

    upload = form.get("final_file")
    filename = str(getattr(upload, "filename", "") or "").strip()
    data = b""
    if upload is not None and hasattr(upload, "read"):
        raw = await upload.read()
        data = bytes(raw or b"")
    if not data:
        return _flash(back, "Choose the Word, PDF, or text file to grade.", "stop")

    criteria = detail.get("evaluation_criteria") or []
    if not criteria:
        return _flash(back, "No evaluation criteria are stored, so this file was not graded.", "stop")

    blocked, why = triggers.aggregate_cap_reached()
    if blocked:
        return _flash(back, why, "cap")

    from processors.downloader import extract_submission_bytes

    extracted = extract_submission_bytes(filename, data)
    if not extracted.get("ok"):
        return _flash(back, str(extracted.get("reason") or "Could not read the file."), "stop")

    from database.supabase_client import queue_final_grade

    ok, reason = queue_final_grade(
        source_url,
        text=extracted["text"],
        filename=extracted.get("filename") or filename,
        uploaded_by=session.get("email") or "",
        criteria=criteria,
    )
    if not ok:
        return _flash(back, reason, "stop")
    return _flash(
        back,
        "The file is queued for grading. This page updates when the reading is stored. Nothing is sent.",
    )


@app.post("/opportunity/outline")
async def opportunity_outline(request: Request, session: dict = Depends(require_session)):
    """Save the chapters for this opportunity. Optionally start the draft.

    Edited headings win over the template id. This does not call a model
    until intent is ``draft``, and then only by queueing the existing worker.
    """
    form = await request.form()
    _check_csrf(session, str(form.get("csrf_token") or ""))
    source_url = str(form.get("source_url") or "").strip()
    back = f"/opportunity?url={quote(source_url, safe='')}" if source_url else "/"
    if not source_url:
        return _flash("/", "Missing the opportunity.", "stop")
    detail = queries.opportunity_detail(source_url)
    if detail is None:
        return _flash("/", "No stored row for that opportunity.", "stop")

    headings = [str(value) for value in form.getlist("heading")]
    limits = [str(value) for value in form.getlist("page_limit")]
    musts = [str(value) for value in form.getlist("must_include")]
    sections = []
    for index, heading in enumerate(headings):
        if not heading.strip():
            continue
        raw_must = musts[index] if index < len(musts) else ""
        sections.append({
            "heading": heading,
            "page_limit": limits[index] if index < len(limits) else "",
            "must_include": [
                part.strip()
                for part in raw_must.replace(";", "\n").split("\n")
                if part.strip()
            ],
        })
    payload = {
        "template_id": str(form.get("template_id") or "from_tor"),
        "submission_type": str(form.get("submission_type") or ""),
        "name": str(form.get("outline_name") or ""),
    }
    if sections:
        payload["sections"] = sections
    try:
        outline = resolve_outline(payload, detail.get("recommended_outline"))
    except ValueError as exc:
        return _flash(back, str(exc), "stop")
    ok, reason = save_accepted_outline(source_url, outline)
    if not ok:
        return _flash(back, reason, "stop")
    if str(form.get("intent") or "") == "draft":
        return _enqueue_one(source_url, triggers.DRAFT_EXISTING, session, back=back)
    return _flash(back, "Outline saved. Write the draft when the chapters look right.")


@app.post("/opportunity/outcome")
def mark_outcome(
    request: Request,
    source_url: str = Form(...),
    outcome: str = Form(...),
    csrf_token: str = Form(""),
    session: dict = Depends(require_session),
):
    """A person records Won or Lost. Does not email or submit."""
    _check_csrf(session, csrf_token)
    if outcome not in ("Won", "Lost"):
        raise HTTPException(status_code=400, detail="outcome must be Won or Lost")
    from database.airtable_client import update_opportunity
    from database.supabase_client import record_human_pipeline_stage

    canonical, back = _detail_back(source_url)
    wrote = update_opportunity(canonical, {"status": outcome})
    staged = record_human_pipeline_stage(canonical, "outcome")
    if wrote and staged:
        return _flash(back, f"Marked {outcome.lower()}.")
    return _flash(back, "Could not record the outcome.", "stop")


# ── thin JSON API (same data as the pages) ──────────────────────────────────

@app.get("/api/queue")
def api_queue(session: dict = Depends(require_session)):
    return {"queue": queries.queue_view(limit=40), "triggers": triggers.recent(12),
            **_spend_context()}


@app.get("/api/opportunity")
def api_opportunity(url: str, session: dict = Depends(require_session)):
    detail = queries.opportunity_detail(url)
    if detail is None:
        raise HTTPException(status_code=404, detail="no stored row for that URL")
    return detail


@app.get("/api/draft-templates")
def api_draft_templates(session: dict = Depends(require_session)):
    """House outlines. The per-ToR recommendation is on GET /api/opportunity."""
    return {"templates": catalog()}


@app.post("/api/opportunity/outline")
async def api_save_outline(request: Request, session: dict = Depends(require_session)):
    """Save the outline for one scored opportunity. Does not draft.

    JSON body: source_url, csrf_token, and either template_id
    (from_tor, house_technical, house_eoi) or sections
    [{heading, page_limit, must_include}]. Optional submission_type
    (FULL_PROPOSAL or EOI) and name. Edited sections replace the template.
    The save belongs to this opportunity.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected a JSON object")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    _check_csrf(session, str(body.get("csrf_token") or ""))
    source_url = str(body.get("source_url") or "").strip()
    if not source_url:
        raise HTTPException(status_code=400, detail="source_url is required")
    detail = queries.opportunity_detail(source_url)
    if detail is None:
        raise HTTPException(status_code=404, detail="no stored row for that URL")
    try:
        outline = resolve_outline(body, detail.get("recommended_outline"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ok, reason = save_accepted_outline(source_url, outline)
    if not ok:
        status = 409 if "in progress" in reason else 400
        raise HTTPException(status_code=status, detail=reason)
    return {"ok": True, "accepted_outline": outline}


@app.get("/api/job/{trigger_id}")
def api_job(trigger_id: str, session: dict = Depends(require_session)):
    """Polled by the job page for browser notifications."""
    job = triggers.get(trigger_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    ledger = queries._ledger_one(job["source_url"]) or {}
    return {
        "job": job,
        "pipeline_stage": (ledger.get("stage") or {}).get("value"),
        "ledger_state": ledger.get("ui_state"),
    }


@app.get("/api/portfolio")
def api_portfolio(session: dict = Depends(require_session)):
    return {**queries.portfolio_view(), **_spend_context()}


# ── knowledge library ───────────────────────────────────────────────────────

def _library_filters(request: Request, session: dict) -> dict:
    params = request.query_params
    tab = str(params.get("tab") or "proposals")
    if tab not in ("proposals", "cvs", "mine", "processing"):
        tab = "proposals"
    filters = {
        "q": str(params.get("q") or ""),
        "donor": str(params.get("donor") or ""),
        "sector": str(params.get("sector") or ""),
        "country": str(params.get("country") or ""),
        "outcome": str(params.get("outcome") or ""),
        "uploader": str(params.get("uploader") or ""),
        "consultant": str(params.get("consultant") or ""),
        "status": str(params.get("status") or ""),
    }
    if tab == "mine":
        filters["uploader"] = str(session.get("email") or "").strip().lower()
    return tab, filters


def _library_client():
    from database.supabase_client import get_supabase

    return get_supabase()


@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    from dashboard import library as library_mod

    session = current_session(request) or {}
    ctx = _base_context(request, "library")
    tab, filters = _library_filters(request, session)
    try:
        supabase = _library_client()
        everything = library_mod.list_documents(supabase)
        rows = library_mod.list_documents(supabase, tab=tab, filters=filters)
        available = True
        reason = ""
        tally = library_mod.counts(everything)
    except Exception as exc:
        rows, everything = [], []
        available = False
        reason = "The library tables are not available yet. Apply supabase_migration_knowledge_library.sql."
        tally = {"proposals_ready": 0, "proposals_processing": 0, "cv_consultants": 0, "cvs_ready": 0}
        logger.warning(f"library page unavailable: {type(exc).__name__}")
    processing = any(row.get("status") in ("uploaded", "queued", "extracting", "embedding") for row in everything)
    ctx.update({
        "tab": tab,
        "rows": rows,
        "counts": tally,
        "available": available,
        "unavailable_reason": reason,
        "library_admin": library_mod.is_admin(session),
        "poll_ms": 5000 if processing else 0,
    })
    return templates.TemplateResponse(request, "library.html", ctx)


@app.get("/api/library")
def api_library(request: Request, session: dict = Depends(require_session)):
    from dashboard import library as library_mod

    tab, filters = _library_filters(request, session)
    rows = library_mod.list_documents(_library_client(), tab=tab, filters=filters)
    return {"tab": tab, "documents": rows, "counts": library_mod.counts(rows)}


@app.get("/api/library/{document_id}")
def api_library_one(document_id: str, session: dict = Depends(require_session)):
    supabase = _library_client()
    found = supabase.table("library_documents").select("*").eq("id", document_id).limit(1).execute()
    rows = found.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="no such document")
    events = supabase.table("library_events").select("*").eq("document_id", document_id).execute()
    return {"document": rows[0], "events": events.data or []}


@app.get("/api/consultants")
def api_consultants(q: str = "", session: dict = Depends(require_session)):
    supabase = _library_client()
    found = supabase.table("consultants").select(
        "id,full_name,role_title,based_in,email"
    ).limit(50).execute()
    rows = found.data or []
    needle = (q or "").strip().lower()
    if needle:
        rows = [
            row for row in rows
            if needle in " ".join(
                str(row.get(field) or "") for field in ("full_name", "role_title", "based_in", "email")
            ).lower()
        ]
    return {"consultants": rows[:20]}


def _meta_from_form(form, index: int) -> dict:
    prefix = f"meta[{index}]"
    keyed = {
        field: form.get(f"{prefix}[{field}]")
        for field in (
            "title", "client", "donor", "country", "sectors",
            "submission_date", "proposal_type", "outcome",
        )
    }
    if any(value not in (None, "") for value in keyed.values()):
        return {key: str(value or "") for key, value in keyed.items()}
    return {
        field: str(form.get(field) or "")
        for field in keyed
    }


def _consultant_for_upload(form, session: dict, supabase, kind: str) -> tuple[str | None, str | None]:
    from dashboard import library as library_mod

    if kind != "cv":
        return None, None
    whose = str(form.get("whose") or "existing").strip().lower()
    if whose == "me":
        row = library_mod.resolve_me(supabase, session)
        return str(row.get("id") or ""), None
    if whose == "new":
        email, _name = library_mod.actor(session)
        created = library_mod.create_consultant(
            supabase,
            {
                "full_name": form.get("new_full_name"),
                "email": form.get("new_email"),
                "role_title": form.get("new_role"),
                "based_in": form.get("new_based_in"),
                "languages": form.get("new_languages"),
                "thematic_expertise": form.get("new_sectors"),
                "years_experience": form.get("new_years"),
            },
            email,
            confirm=str(form.get("confirm_name") or "") in ("1", "true", "on"),
        )
        if created.get("did_you_mean") and not created.get("consultant"):
            names = ", ".join(created["did_you_mean"])
            return None, f"Did you mean {names}?"
        return str((created.get("consultant") or {}).get("id") or ""), None
    consultant_id = str(form.get("consultant_id") or "").strip()
    if not consultant_id:
        return None, "Choose whose CV this is: Me, someone on the team, or a new team member"
    return library_mod.check_consultant_id(supabase, consultant_id)


@app.post("/library/upload")
async def library_upload(request: Request, session: dict = Depends(require_session)):
    from dashboard import library as library_mod

    form = await request.form()
    _check_csrf(session, str(form.get("csrf_token") or ""))
    kind = str(form.get("kind") or "").strip().lower()
    uploads = [item for item in form.getlist("files") if getattr(item, "filename", None)]
    wants_json = "application/json" in str(request.headers.get("accept") or "")
    if len(uploads) > library_mod.MAX_FILES:
        message = "Upload 10 files or fewer at a time."
        if wants_json:
            return JSONResponse({"ok": False, "reason": message}, status_code=400)
        return _flash("/library", message, "stop")
    if not uploads:
        message = "Choose a PDF or DOCX file."
        if wants_json:
            return JSONResponse({"ok": False, "reason": message}, status_code=400)
        return _flash("/library", message, "stop")
    try:
        supabase = _library_client()
        consultant_id, consultant_error = _consultant_for_upload(form, session, supabase, kind)
    except Exception as exc:
        message = str(exc) or "The consultant could not be saved."
        if wants_json:
            return JSONResponse({"ok": False, "reason": message}, status_code=400)
        return _flash("/library", message, "stop")
    if consultant_error:
        if wants_json:
            return JSONResponse({"ok": False, "reason": consultant_error}, status_code=400)
        return _flash("/library", consultant_error, "stop")
    results = []
    for index, item in enumerate(uploads):
        data = await item.read()
        results.append(library_mod.store_upload(
            supabase,
            session,
            kind=kind,
            filename=str(item.filename),
            data=bytes(data or b""),
            meta=_meta_from_form(form, index),
            consultant_id=consultant_id,
        ))
    if wants_json:
        return {"ok": any(row.get("ok") for row in results), "results": results}
    ok = [row for row in results if row.get("ok")]
    bad = [row for row in results if not row.get("ok")]
    message = f"Added {len(ok)} file(s)."
    if bad:
        message += " " + "; ".join(str(row.get("reason") or "not added") for row in bad[:4])
    return _flash("/library", message, "" if ok else "stop")


@app.post("/library/{document_id}/update")
async def library_update(document_id: str, request: Request, session: dict = Depends(require_session)):
    form = await request.form()
    _check_csrf(session, str(form.get("csrf_token") or ""))
    supabase = _library_client()
    found = supabase.table("library_documents").select("*").eq("id", document_id).limit(1).execute()
    rows = found.data or []
    if not rows:
        return _flash("/library", "That document is not in the library.", "stop")
    current = rows[0]
    outcome = str(form.get("outcome") or current.get("outcome") or "unknown").strip().lower()
    if outcome not in ("won", "lost", "pending", "unknown"):
        outcome = "unknown"
    patch = {
        "title": str(form.get("title") or current.get("title") or ""),
        "client": str(form.get("client") or "").strip() or None,
        "donor": str(form.get("donor") or "").strip() or None,
        "country": str(form.get("country") or "").strip() or None,
        "outcome": outcome,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    supabase.table("library_documents").update(patch).eq("id", document_id).execute()
    if outcome != str(current.get("outcome") or "") and current.get("kind") == "proposal":
        from dashboard import library as library_mod

        triggers.enqueue(library_mod.library_source(document_id), triggers.LIBRARY_INGEST, session.get("email") or "")
        supabase.table("library_documents").update({"status": "queued"}).eq("id", document_id).execute()
    return _flash("/library", "Document details saved.")


@app.post("/library/{document_id}/reprocess")
async def library_reprocess(document_id: str, request: Request, session: dict = Depends(require_session)):
    from dashboard import library as library_mod

    form = await request.form()
    _check_csrf(session, str(form.get("csrf_token") or ""))
    if not settings.TRIGGERS_ENABLED:
        return _flash("/library", "Read-only: new runs are paused", "stop")
    queued = triggers.enqueue(
        library_mod.library_source(document_id), triggers.LIBRARY_INGEST, session.get("email") or ""
    )
    if not queued:
        return _flash("/library", "This document is already queued.", "stop")
    _library_client().table("library_documents").update({"status": "queued"}).eq("id", document_id).execute()
    return _flash("/library", "Reprocessing this document.")


@app.post("/library/{document_id}/archive")
async def library_archive(document_id: str, request: Request, session: dict = Depends(require_session)):
    from dashboard import library as library_mod

    form = await request.form()
    _check_csrf(session, str(form.get("csrf_token") or ""))
    ok, message = library_mod.archive_document(_library_client(), session, document_id)
    if not ok and "archive a document you uploaded" in message:
        raise HTTPException(status_code=403, detail=message)
    return _flash("/library", message, "" if ok else "stop")


@app.post("/library/{document_id}/restore")
async def library_restore(document_id: str, request: Request, session: dict = Depends(require_session)):
    from dashboard import library as library_mod

    form = await request.form()
    _check_csrf(session, str(form.get("csrf_token") or ""))
    ok, message = library_mod.restore_document(_library_client(), session, document_id)
    if not ok:
        raise HTTPException(status_code=403, detail=message)
    return _flash("/library", message)


@app.post("/consultants/new")
async def consultants_new(request: Request, session: dict = Depends(require_session)):
    from dashboard import library as library_mod

    form = await request.form()
    _check_csrf(session, str(form.get("csrf_token") or ""))
    email, _name = library_mod.actor(session)
    try:
        created = library_mod.create_consultant(
            _library_client(),
            {
                "full_name": form.get("full_name"),
                "email": form.get("email"),
                "role_title": form.get("role_title"),
                "based_in": form.get("based_in"),
                "languages": form.get("languages"),
                "thematic_expertise": form.get("thematic_expertise"),
                "years_experience": form.get("years_experience"),
            },
            email,
            confirm=str(form.get("confirm_name") or "") in ("1", "true", "on"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    wants_json = "application/json" in str(request.headers.get("accept") or "")
    if wants_json:
        return created
    if created.get("did_you_mean") and not created.get("consultant"):
        return _flash("/library", "Did you mean " + ", ".join(created["did_you_mean"]) + "?", "stop")
    return _flash("/library", "Team member saved.")


@app.get("/library/{document_id}/file")
def library_file(document_id: str, session: dict = Depends(require_session)):
    from dashboard.library import BUCKET

    supabase = _library_client()
    found = supabase.table("library_documents").select("storage_path").eq("id", document_id).limit(1).execute()
    rows = found.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="no such document")
    signed = supabase.storage.from_(BUCKET).create_signed_url(rows[0]["storage_path"], 60)
    url = ""
    if isinstance(signed, dict):
        url = signed.get("signedURL") or signed.get("signed_url") or ""
    if not url:
        raise HTTPException(status_code=404, detail="the original could not be signed")
    return RedirectResponse(url, status_code=302)
