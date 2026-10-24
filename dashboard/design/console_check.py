"""Console, network, overflow and link check over every harness page.

    .venv/bin/python dashboard/design/console_check.py

Runs the fixtures-patched app on 127.0.0.1:8765 (never 8790), opens each page at
1440, 1024 and 390 with motion on, and reports console errors and warnings,
uncaught page errors, failed or non-local requests, horizontal overflow, and
same-origin GET links that do not answer below 400. Exit code 1 on any finding.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dashboard.design import ui_harness as H  # noqa: E402  (sets the harness session secret)

PORT = 8765
# Logout and Google sign-in have side effects; library file and detail routes need real storage
# (the harness has none), and are covered by tests/test_library.py instead.
# Pages whose previewed state is an error response on purpose: the denied page is a 403, sign-in is
# a 503 while Google sign-in is unconfigured in the harness, and library-error injects a 400.
EXPECTED = {"denied": 403, "login": 503, "library-error": 400}
SKIP_LINK = ("/auth/logout", "/auth/google", "/healthz", "/api/library/", "/library/")


def main() -> int:
    from playwright.sync_api import sync_playwright

    from dashboard import auth, settings

    settings.SESSION_SECRET = os.environ["DASHBOARD_SESSION_SECRET"]
    token = auth.issue_session("staff@cortechconsultinggroup.com")
    proc = subprocess.Popen([sys.executable, str(ROOT / "dashboard/design/ui_harness.py"), "serve", str(PORT)], cwd=str(ROOT))
    findings: list[str] = []
    seen_links: dict[str, int] = {}
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        origin = f"http://127.0.0.1:{PORT}"
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            for width, height in ((1440, 900), (1024, 768), (390, 844)):
                ctx = browser.new_context(viewport={"width": width, "height": height}, reduced_motion="no-preference")
                ctx.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": token, "url": origin}])
                anon = browser.new_context(viewport={"width": width, "height": height}, reduced_motion="no-preference")
                for name, path in H.PAGES:
                    c = anon if name == "login" else ctx
                    page = c.new_page()
                    tag = f"{name}@{width}"

                    def on_console(m, tag=tag):
                        if m.type in ("error", "warning"):
                            findings.append(f"{tag} console {m.type}: {m.text[:160]}")

                    page.on("console", on_console)
                    page.on("pageerror", lambda e, tag=tag: findings.append(f"{tag} page error: {str(e)[:160]}"))
                    page.on("requestfailed", lambda r, tag=tag: findings.append(f"{tag} request failed: {r.url[:120]}"))
                    page.on("request", lambda r, tag=tag: findings.append(f"{tag} external request: {r.url[:120]}")
                            if r.url.startswith("http") and not r.url.startswith(origin) else None)
                    page.on("response", lambda r, tag=tag: findings.append(f"{tag} HTTP {r.status}: {r.url[:120]}")
                            if r.url.startswith(origin) and r.status >= 400 and "/api/consultants" not in r.url and not name.endswith("error") else None)
                    page.goto(origin + path, wait_until="networkidle")
                    page.evaluate("document.fonts.ready")
                    if name in H.STEPS:
                        try:
                            H.STEPS[name](page)
                        except Exception as exc:  # a broken step is itself a finding
                            findings.append(f"{tag} step failed: {str(exc)[:120]}")
                    for y in range(0, 6000, 700):
                        page.evaluate(f"window.scrollTo({{top:{y}, behavior:'instant'}})")
                        page.wait_for_timeout(60)
                    over = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    if over > 1:
                        wide = page.evaluate("""() => [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > document.documentElement.clientWidth + 1 && !e.closest('.table-wrap, .sr-only')).slice(0, 3).map(e => e.tagName + '.' + e.className)""")
                        findings.append(f"{tag} horizontal overflow {over}px {wide}")
                    if width == 1440:
                        for href in page.evaluate("[...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href'))"):
                            u = urlparse(href)
                            if u.scheme or href.startswith("#") or href.startswith("mailto:") or any(href.startswith(s) for s in SKIP_LINK):
                                continue
                            if href not in seen_links:
                                r = ctx.request.get(origin + href, max_redirects=0)
                                seen_links[href] = r.status
                                if r.status >= 400:
                                    findings.append(f"{tag} broken link {href} -> {r.status}")
                    page.close()
                ctx.close()
                anon.close()
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    def expected(f: str) -> bool:
        name = f.split("@", 1)[0]
        code = EXPECTED.get(name)
        return bool(code) and (f"HTTP {code}:" in f or f"status of {code}" in f)

    findings = [f for f in findings if not expected(f)]
    if findings:
        print("\n".join(sorted(set(findings))))
    print(f"{len(H.PAGES)} pages x 3 widths, {len(seen_links)} local links checked, {len(set(findings))} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
