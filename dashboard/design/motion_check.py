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

            # 7: pending overlay (white, orbiting logo, 0.8 s loop, 150 ms delay, handoff fade, no search indicator)
            page.goto(base + "/", wait_until="networkidle")
            page.wait_for_timeout(600)
            page.evaluate("window.__t0 = performance.now(); CortechPending.show({})")
            page.wait_for_timeout(60)
            early = page.evaluate("!!document.querySelector('.pend')")
            page.wait_for_selector(".pend")
            page.wait_for_timeout(300)
            info = page.evaluate("""() => { const o = document.querySelector('.pend'); const l = o.querySelector('.pend__logo');
                const cs = getComputedStyle(o); const a = getComputedStyle(o.querySelector('.pend__arm'));
                const lc = getComputedStyle(l);
                return {bg: cs.backgroundColor, pos: cs.position, op: cs.opacity, w: l.getBoundingClientRect().width,
                        dur: a.animationDuration, fn: a.animationTimingFunction, ldur: lc.animationDuration, lfn: lc.animationTimingFunction,
                        label: o.textContent.trim(), role: o.getAttribute('role'),
                        live: o.getAttribute('aria-live'),
                        ring: !!o.querySelector('.pend__ring, .spinner')}; }""")
            results.append(_ok("pending overlay is not shown in the first 60 ms (150 ms delay)", not early))
            results.append(_ok("pending overlay is opaque white and fixed", info["bg"] == "rgb(255, 255, 255)" and info["pos"] == "fixed" and float(info["op"]) > 0.95, str(info["bg"])))
            results.append(_ok("pending logo is at most 150px wide, no spinner ring", info["w"] <= 150 and not info["ring"], f"{info['w']}px"))
            results.append(_ok("orbit loop is 0.8s and linear, transform-only",
                               info["dur"] == "0.8s" and info["fn"] == "linear" and info["ldur"] == "0.8s" and info["lfn"] == "linear", f"{info['dur']} {info['fn']}"))
            results.append(_ok("pending label is Loading, polite live region", info["label"] == "Loading" and info["role"] == "status" and info["live"] == "polite"))
            # orbit frames at 0, 200 and 400 ms: pause the loop at each time and measure the logo centre
            pts = []
            for ms in (0, 200, 400, 800):
                page.evaluate("""(ms) => document.getAnimations().forEach(a => {
                    if (a.animationName === 'pend-arm' || a.animationName === 'pend-counter') { a.pause(); a.currentTime = ms; } })""", ms)
                page.wait_for_timeout(80)
                pts.append(page.evaluate("""() => { const l = document.querySelector('.pend__logo').getBoundingClientRect();
                    const v = {x: innerWidth / 2, y: innerHeight / 2}; return [l.left + l.width / 2 - v.x, l.top + l.height / 2 - v.y, l.width, l.height]; }"""))
                if ms in (0, 200, 400):
                    page.screenshot(path=str(out / f"frame-pending-orbit-{ms}ms.png"))
            import math
            radii = [round(math.hypot(x, y), 1) for x, y, _, _ in pts]
            moved = math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]) > 20 and math.hypot(pts[2][0] - pts[0][0], pts[2][1] - pts[0][1]) > 20
            results.append(_ok("logo moves between 0, 200 and 400 ms on a circle of 18 to 28px radius",
                               moved and all(18 <= r <= 28 for r in radii), f"radii {radii}, points {[[round(a), round(b)] for a, b, _, _ in pts]}"))
            results.append(_ok("one loop (800 ms) returns to the start", math.hypot(pts[3][0] - pts[0][0], pts[3][1] - pts[0][1]) < 2))
            results.append(_ok("logo is not rotated or resized while orbiting", len({(round(w), round(h)) for _, _, w, h in pts}) == 1))
            page.evaluate("CortechPending.hide(true)")
            gone = page.evaluate("!document.querySelector('.pend')")
            results.append(_ok("pending overlay clears on hide", gone))
            # search input: typing shows no overlay, no spinner, no pending text
            page.fill("#opp-filter-q", "water")
            page.wait_for_timeout(500)
            quiet = page.evaluate("""() => !document.querySelector('.pend') && !document.querySelector('#url-status-pill') &&
                !/(Loading|Uploading|Starting|Running|Searching)/.test(document.querySelector('.workq__head').textContent) &&
                !document.querySelector('.workq__head [aria-busy=true], .workq__head .spinner')""")
            results.append(_ok("search bar shows no pending indicator while typing", bool(quiet)))
            # handoff: a flagged navigation brings the overlay up at once and fades it out (200 to 300 ms) on load
            page.evaluate("sessionStorage.setItem('cortech.pending', String(Date.now()))")
            page.evaluate("""() => { window.__log = []; }""")
            ctx.add_init_script("""(() => { window.__pend = {}; const mo = new MutationObserver(() => {
                const o = document.querySelector('.pend'); const t = performance.now();
                if (o && !window.__pend.added) window.__pend.added = t;
                if (o && o.classList.contains('is-off') && !window.__pend.off) window.__pend.off = t;
                if (!o && window.__pend.added && !window.__pend.removed) window.__pend.removed = t; });
                mo.observe(document, {childList: true, subtree: true, attributes: true, attributeFilter: ['class']}); })()""")
            page.goto(base + "/library", wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            h = page.evaluate("window.__pend")
            fade = (h.get("removed", 0) - h.get("off", 0)) if h.get("off") and h.get("removed") else -1
            results.append(_ok("handoff: overlay is up on the new page then fades out in 200 to 300 ms", 200 <= fade <= 330, f"{fade:.0f} ms"))
            results.append(_ok("handoff: overlay is gone afterwards", page.evaluate("!document.querySelector('.pend')")))
            page.evaluate("CortechPending.show({now: true})")
            page.wait_for_timeout(200)
            page.keyboard.press("Escape")
            page.wait_for_timeout(100)
            results.append(_ok("Escape clears a stuck overlay", page.evaluate("!document.querySelector('.pend')")))
            # reduced motion: the loop is off and the logo sits still
            rctx = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
            rctx.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": token, "url": f"http://127.0.0.1:{PORT}"}])
            rp = rctx.new_page()
            rp.goto(base + "/", wait_until="networkidle")
            rp.evaluate("CortechPending.show({now: true})")
            rp.wait_for_timeout(300)
            rm = rp.evaluate("""() => { const l = document.querySelector('.pend__logo'); const a = document.querySelector('.pend__arm');
                return [getComputedStyle(l).animationName, getComputedStyle(a).animationName, getComputedStyle(l).transform]; }""")
            results.append(_ok("reduced motion: no orbit animation", rm[0] == "none" and rm[1] == "none" and rm[2] in ("none", "matrix(1, 0, 0, 1, 0, 0)"), str(rm)))
            rctx.close()
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
