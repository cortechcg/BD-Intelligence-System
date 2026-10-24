"""Animation check for the v3 dashboard. Sample fixtures only; nothing live.

    .venv/bin/python dashboard/design/motion_check.py [outdir]

Starts the fixtures-patched harness app on 127.0.0.1:8765, opens Track record,
the Library and the Pipeline with motion on, samples what is animating and
prints PASS or FAIL lines and saves a few frames to outdir (default
output/ui-v3/motion). Add --video to also save a .webm (needs
`playwright install ffmpeg` once). The run is slow enough to screen-record as is.

What animates:
  1. Headline: each word rises and fades in, one after another (about 40 ms apart).
  2. Figures: whole numbers count up to the real value, then stop.
  3. Sections and cards: fade up once as they scroll into view, in a short stagger.
  4. Masthead: a soft shadow appears once the page has scrolled.
  5. Library: "Add a CV or past proposal" opens the drop panel with a short ease.
  6. Hover and focus: rows, tiles and cards tint or lift, focus rings ease in.
With prefers-reduced-motion, none of it moves and everything shows at once.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["DASHBOARD_SESSION_SECRET"] = "ui-harness-secret-not-real"  # same value the harness app uses

PORT = 8765  # never 8790


def _ok(label: str, cond: bool, detail: str = "") -> bool:
    print(("PASS  " if cond else "FAIL  ") + label + (f"  ({detail})" if detail else ""))
    return cond


def main(outdir: str) -> int:
    from playwright.sync_api import sync_playwright

    from dashboard import auth, settings

    settings.SESSION_SECRET = os.environ["DASHBOARD_SESSION_SECRET"]
    token = auth.issue_session("staff@cortechconsultinggroup.com")
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, str(ROOT / "dashboard/design/ui_harness.py"), "serve", str(PORT)], cwd=str(ROOT))
    results: list[bool] = []
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            opts = {"viewport": {"width": 1440, "height": 900}, "reduced_motion": "no-preference"}
            if "--video" in sys.argv:
                opts.update(record_video_dir=str(out), record_video_size={"width": 1440, "height": 900})
            ctx = browser.new_context(**opts)
            ctx.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": token, "url": f"http://127.0.0.1:{PORT}"}])
            page = ctx.new_page()
            base = f"http://127.0.0.1:{PORT}"

            # 1 and 2: headline words and count-up on Track record
            page.goto(base + "/track-record", wait_until="domcontentloaded")
            page.wait_for_timeout(60)
            early = page.evaluate("""() => {
              const w = [...document.querySelectorAll('.tr-hero h1 .w')];
              const n = document.querySelector('.tr-figures .tr-fig__value');
              return {words: w.length, opacities: w.map(x => +getComputedStyle(x).opacity), count: n ? n.textContent.trim() : null};
            }""")
            page.screenshot(path=str(out / "frame-1-words-rising.png"))
            page.wait_for_timeout(1800)
            page.screenshot(path=str(out / "frame-2-settled.png"))
            late = page.evaluate("""() => {
              const w = [...document.querySelectorAll('.tr-hero h1 .w')];
              const n = document.querySelector('.tr-figures .tr-fig__value');
              return {opacities: w.map(x => +getComputedStyle(x).opacity), count: n ? n.textContent.trim() : null};
            }""")
            results.append(_ok("headline is split into words", early["words"] >= 3, f"{early['words']} words"))
            results.append(_ok("words reveal in turn", min(early["opacities"] or [1]) < 1 and min(late["opacities"] or [0]) == 1,
                               f"early min {min(early['opacities'] or [1]):.2f}, late min {min(late['opacities'] or [0]):.2f}"))
            results.append(_ok("first figure ends on its real value", late["count"] not in (None, "", "0"), str(late["count"])))

            # 3 and 4: scroll reveals and the masthead shadow
            hidden_before = page.evaluate("document.querySelectorAll('.rv:not(.rv-in)').length")
            for y in range(0, 4200, 350):
                page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'instant'}})")
                page.wait_for_timeout(260)
            hidden_after = page.evaluate("document.querySelectorAll('.rv:not(.rv-in)').length")
            stuck = page.evaluate("!!document.querySelector('.masthead.is-stuck')")
            results.append(_ok("blocks wait for the scroll, then reveal", hidden_before > 0 and hidden_after < hidden_before,
                               f"{hidden_before} waiting, {hidden_after} left"))
            results.append(_ok("masthead shadow appears on scroll", stuck))

            # 5: library panel
            page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
            page.goto(base + "/library", wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.click("[data-lib-open]")
            page.wait_for_timeout(700)
            opened = page.evaluate("(() => { const el = document.querySelector('[data-lib-panel]'); return el && el.classList.contains('is-open') && getComputedStyle(el).display !== 'none'; })()")
            results.append(_ok("library panel opens", bool(opened)))
            page.screenshot(path=str(out / "frame-3-library-panel.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

            # 6: hover transition on a pipeline row
            page.goto(base + "/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            row = page.query_selector(".table tbody tr")
            if row:
                before = row.eval_on_selector("td", "el => getComputedStyle(el).backgroundColor")
                row.hover()
                page.wait_for_timeout(500)
                after = row.eval_on_selector("td", "el => getComputedStyle(el).backgroundColor")
                results.append(_ok("row tints on hover", before != after, f"{before} to {after}"))
            page.wait_for_timeout(800)
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    print(f"frames saved in {out}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main(next((a for a in sys.argv[1:] if not a.startswith("--")), str(ROOT / "output/ui-v3/motion"))))
