#!/usr/bin/env python3
"""Real Uvicorn + HTTPX checks against the in-memory visual fixture app.

No production database, portal, mail, or proposal action is touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["DASHBOARD_SESSION_SECRET"] = "ui-harness-secret-not-real"


def check(label: str, condition: bool) -> None:
    print(("PASS " if condition else "FAIL ") + label, flush=True)
    if not condition:
        raise SystemExit(1)


def main(port: int = 18897) -> int:
    from dashboard import auth, settings

    settings.SESSION_SECRET = os.environ["DASHBOARD_SESSION_SECRET"]
    env = dict(os.environ)
    env["DASHBOARD_SESSION_SECRET"] = settings.SESSION_SECRET
    env["RENDER_GIT_COMMIT"] = "fixture-commit-sha"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "dashboard/design/ui_harness.py"), "serve", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(40):
            try:
                with httpx.Client(timeout=1.5) as probe:
                    if probe.get(base + "/healthz").status_code == 200:
                        break
            except httpx.HTTPError:
                time.sleep(.25)
        else:
            check("fixture Uvicorn health", False)

        with httpx.Client(base_url=base, timeout=5, follow_redirects=False) as client:
            health = client.get("/healthz")
            check("health endpoint returns the fixture commit identity", health.status_code == 200 and health.text == "ok" and health.headers.get("X-Cortech-Git-Commit") == "fixture-commit-sha")
            guest = client.get("/sources")
            check("unauthenticated Sources redirects to sign-in", guest.status_code == 302 and guest.headers.get("location") == "/auth/login")

            client.cookies.set(settings.SESSION_COOKIE_NAME, auth.issue_session("external@example.net"))
            restricted = client.get("/sources")
            check("restricted email remains unauthenticated", restricted.status_code == 302 and restricted.headers.get("location") == "/auth/login")

            client.cookies.set(settings.SESSION_COOKIE_NAME, auth.issue_session("staff@cortechconsultinggroup.com"))
            sources = client.get("/sources")
            check("authorized Sources page renders labeled synthetic telemetry", sources.status_code == 200 and "FIXTURE PREVIEW" in sources.text and "County procurement feed" in sources.text)
            bad_source_write = client.post("/sources/add", data={"name": "fixture", "url": "https://example.org/feed.xml", "csrf_token": "invalid"})
            check("source mutation rejects invalid CSRF before registry access", bad_source_write.status_code == 403)

            proposal_url = "https://tenders.example.org/giz-climate-resilience-evaluation"
            review = client.get("/opportunity", params={"url": proposal_url})
            check("authorized proposal review renders rubric and preserved version history", review.status_code == 200 and "Simulated internal rubric review" in review.text and "Preserved prior drafts" in review.text)
            bad_edit = client.post("/opportunity/draft/edit", data={"source_url": proposal_url, "section_key": "methodology", "content": "fixture edit", "csrf_token": "invalid"})
            check("proposal edit rejects invalid CSRF before persistence", bad_edit.status_code == 403)
            bad_approval = client.post("/opportunity/release-approval", data={"source_url": proposal_url, "csrf_token": "invalid"})
            check("release approval rejects invalid CSRF before persistence", bad_approval.status_code == 403)

            unauth_api = httpx.get(base + "/api/queue", timeout=5)
            check("unauthenticated JSON API remains 401", unauth_api.status_code == 401)

            css = client.get("/static/pending.css")
            js = client.get("/static/pending.js")
            logo = client.get("/static/brand/cortech-logo.png")
            check("pending CSS and JS assets load", css.status_code == 200 and js.status_code == 200)
            check("authentic logo asset loads as PNG", logo.status_code == 200 and logo.headers.get("content-type", "").startswith("image/png"))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 18897))
