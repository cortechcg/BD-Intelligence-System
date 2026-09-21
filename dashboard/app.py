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

from dashboard import auth, queries, settings, triggers
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


app = FastAPI(
    title="Cortech BD control", docs_url=None, redoc_url=None, openapi_url=None,
    lifespan=_lifespan,
)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))

PUBLIC_PATHS = {"/auth/login", "/auth/callback", "/auth/denied", "/healthz"}

#: The one description of what Cancel does. Rendered verbatim in the UI and
#: asserted by tests/test_dashboard_controls.py against the code path, so the
#: copy cannot drift from the behaviour.
CANCEL_COPY = (
    "Cancel is cooperative, not instant. A job that has not started is "
    "withdrawn immediately and the worker never sees it. A job that is running "
    "is flagged; the worker's next heartbeat (every "
    f"{int(settings.HEARTBEAT_SECONDS)}s) asks the pipeline to halt before its "
    "next model call. The model call already in flight finishes — up to "
    "ANTHROPIC_TIMEOUT_SECONDS — and its cost is incurred and recorded. The "
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
        "url_queue": "/",
        "url_portfolio": "/portfolio",
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
        "cancel_copy": CANCEL_COPY,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "flash": request.query_params.get("msg", ""),
        "flash_kind": request.query_params.get("kind", ""),
    }


# ── auth routes ─────────────────────────────────────────────────────────────

@app.get("/auth/login")
def login(request: Request):
    if current_session(request):
        return RedirectResponse("/", status_code=302)
    try:
        url, state_cookie = auth.begin_login()
    except auth.AuthError as exc:
        return PlainTextResponse(str(exc), status_code=503)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        auth.STATE_COOKIE, state_cookie, max_age=600, httponly=True,
        secure=settings.COOKIE_SECURE, samesite="lax", path="/auth",
    )
    return response


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return PlainTextResponse(f"Google returned an error: {error}", status_code=400)
    try:
        claims = auth.complete_login(code, state, request.cookies.get(auth.STATE_COOKIE))
    except auth.AuthError as exc:
        # 403 and the specific reason: a login that fails opaquely gets
        # worked around rather than fixed.
        return PlainTextResponse(str(exc), status_code=403)

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
            "the dashboard_triggers table is missing or out of date — apply "
            "supabase_migration_dashboard_triggers.sql and "
            "supabase_migration_dashboard_controls.sql"
        )
    return True, ""


def _spend_context() -> dict:
    window = triggers.spend_window()
    blocked, reason = triggers.aggregate_cap_reached(window)
    return {"spend": window, "cap_blocked": blocked, "cap_reason": reason}


@app.get("/", response_class=HTMLResponse)
def queue_page(request: Request):
    enabled, reason = _triggering_state()
    ctx = _base_context(request, "queue")
    ctx.update({
        "queue": queries.queue_view(limit=40),
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
        "org": queries.organization_history(client_name),
        "jobs": jobs,
        "active_job": active,
        # Poll faster while a job for this opportunity is in flight.
        "poll_ms": 5000 if active else settings.POLL_MS,
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
    stage_ramp = ["#6291e3", "#406cbb", "#1e4994", "#002972"]
    rec_ramp = ["#c3a35f", "#947630", "#705300"]
    ctx = _base_context(request, "portfolio")
    ctx.update({
        "p": portfolio,
        "stage_bars": queries.bar_rows(
            [(s, portfolio["by_stage"][s]) for s in queries.AGENT_STAGES], stage_ramp
        ),
        "rec_bars": queries.bar_rows(
            [(k, portfolio["by_recommendation"].get(k, 0)) for k in ("BID", "WATCH", "NO-BID")],
            rec_ramp,
        ),
        "digest": queries.market_digest(),
        "dashboard_runs": triggers.recent(50),
        **_spend_context(),
    })
    return templates.TemplateResponse(request, "portfolio.html", ctx)


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
        "# DRAFT — INTERNAL REVIEW ONLY", "",
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
        raise HTTPException(status_code=403, detail="stale form — reload and retry")


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
        return _flash(back, f"Not queued — {why}", "cap")

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
        return _flash(back, "Already queued or running — nothing was started twice.")
    return RedirectResponse(f"/job/{trigger_id}", status_code=303)


@app.post("/trigger/url")
def trigger_url(
    request: Request, url: str = Form(...), csrf_token: str = Form(""),
    session: dict = Depends(require_session),
):
    _check_csrf(session, csrf_token)
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
        return _flash("/", f"Not queued — {why}", "cap")

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
        "cancelled_before_start": "Cancelled before it started — the worker will not run it.",
        "halt_requested": (
            "Halt requested. The worker stops before its next model call; the call "
            "in flight finishes and its cost is recorded."
        ),
        "not_found": "No such job.",
    }.get(outcome, f"Nothing to cancel ({outcome.replace('already_', 'job is ')}).")
    return _flash(f"/job/{trigger_id}", msg, "stop" if outcome == "not_found" else "")


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
