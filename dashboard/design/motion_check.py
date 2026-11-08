"""Animation check for the v3 dashboard. Sample fixtures only; nothing live.

    .venv/bin/python dashboard/design/motion_check.py [outdir]

Starts the fixtures-patched harness app on 127.0.0.1:8765, opens Track record,
the Library and the Pipeline with motion on, samples what is animating and
prints PASS or FAIL lines and saves a few frames to outdir (default
output/ui-v3/motion). Add --video to save a short pending-animation .webm.

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

PORT = int(os.environ.get("MOTION_CHECK_PORT", "8765"))


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

            # 7: shared pending component, actual job states, cleanup and reduced motion.
            page.goto(base + "/", wait_until="networkidle")
            page.wait_for_timeout(600)
            page.evaluate("CortechPending.show({label: 'Opening page'})")
            page.wait_for_timeout(60)
            early = page.evaluate("!!document.querySelector('.pend')")
            page.wait_for_selector(".pend")
            page.wait_for_timeout(260)
            page.evaluate("document.getAnimations().filter(a=>a.animationName==='pend-beat').forEach(a=>{a.pause();a.currentTime=0})")
            info = page.evaluate("""() => { const o = document.querySelector('.pend'); const l = o.querySelector('.pend__logo');
                const cs = getComputedStyle(o); const a = getComputedStyle(l);
                const card = getComputedStyle(o.querySelector('.pend__card'));
                return {bg: cs.backgroundColor, pos: cs.position, op: cs.opacity, w: l.getBoundingClientRect().width,
                        dur: a.animationDuration, fn: a.animationTimingFunction,
                        label: o.textContent.trim(), role: o.getAttribute('role'),
                        live: o.getAttribute('aria-live'), atomic: o.getAttribute('aria-atomic'),
                        logoLoaded: l.complete && l.naturalWidth === 180,
                        cardRadius: card.borderRadius,
                        ring: !!o.querySelector('.pend__ring, .spinner')}; }""")
            results.append(_ok("pending overlay waits 150 ms before showing", not early))
            results.append(_ok("pending overlay uses dark backdrop and fixed positioning", info["bg"] == "rgba(5, 11, 8, 0.84)" and info["pos"] == "fixed" and float(info["op"]) > 0.95, str(info["bg"])))
            results.append(_ok("official logo loads at its crisp 132px display width", info["logoLoaded"] and info["w"] == 132, f"{info['w']}px, loaded={info['logoLoaded']}"))
            results.append(_ok("rounded dark panel and no spinner ring", info["cardRadius"] == "18px" and not info["ring"]))
            results.append(_ok("pending logo uses a gentle 1.7s double-beat pulse",
                               info["dur"] == "1.7s" and info["fn"] == "ease-in-out", f"{info['dur']} {info['fn']}"))
            results.append(_ok("label is specific, polite and announced atomically",
                               info["label"] == "Opening page" and info["role"] == "status" and info["live"] == "polite" and info["atomic"] == "true"))
            # Pause the real CSS animation at the two beat peaks and rest phase.
            scales = []
            for ms in (0, 100, 200, 340, 600, 1700):
                page.evaluate("""(ms) => document.getAnimations().forEach(a => {
                    if (a.animationName === 'pend-beat') { a.pause(); a.currentTime = ms; } })""", ms)
                scales.append(page.evaluate("""() => { const t=getComputedStyle(document.querySelector('.pend__logo')).transform;
                    return t === 'none' ? 1 : new DOMMatrix(t).a; }"""))
                if ms in (0, 100, 340):
                    page.screenshot(path=str(out / f"frame-pending-double-beat-{ms}ms.png"))
            results.append(_ok("both pulse beats are subtle and scale returns to rest",
                               1.02 < scales[1] <= 1.04 and 1.015 < scales[3] <= 1.035 and abs(scales[0]-1) < .005 and abs(scales[-1]-1) < .005,
                               str([round(x, 3) for x in scales])))
            page.evaluate("CortechPending.hide(true)")
            gone = page.evaluate("!document.querySelector('.pend')")
            results.append(_ok("completion clears overlay and body busy state", gone and page.evaluate("!document.body.hasAttribute('aria-busy')")))

            # Scope lifecycle for a real panel operation; no production request is made.
            page.goto(base + "/library", wait_until="networkidle")
            page.evaluate("CortechPending.show({now:true, scope:document.querySelector('.lib-panel'), label:'Uploading files to the library'})")
            scoped = page.evaluate("document.querySelector('.lib-panel').getAttribute('aria-busy') === 'true' && document.querySelector('.pend__label').textContent === 'Uploading files to the library'")
            page.evaluate("CortechPending.hide(true)")
            cleaned = page.evaluate("!document.querySelector('.lib-panel').hasAttribute('aria-busy') && !document.querySelector('.lib-panel').classList.contains('pend-host')")
            results.append(_ok("scoped pending label and busy semantics are correct", scoped))
            results.append(_ok("panel completion removes busy state without layout residue", cleaned))

            # Search stays quiet; a JavaScript error clears the global overlay.
            page.goto(base + "/", wait_until="networkidle")
            page.fill("#opp-filter-q", "water")
            page.wait_for_timeout(500)
            quiet = page.evaluate("""() => !document.querySelector('.pend') && !document.querySelector('#url-status-pill') &&
                !/(Loading|Uploading|Starting|Running|Searching)/.test(document.querySelector('.workq__head').textContent) &&
                !document.querySelector('.workq__head [aria-busy=true], .workq__head .spinner')""")
            results.append(_ok("search bar shows no pending indicator while typing", bool(quiet)))
            page.evaluate("CortechPending.show({now:true})")
            page.evaluate("window.dispatchEvent(new ErrorEvent('error', {message:'fixture error'}))")
            results.append(_ok("script error clears pending state", page.evaluate("!document.querySelector('.pend') && !document.body.hasAttribute('aria-busy')")))

            # Real fixture-backed states: queued and terminal jobs stay static, only
            # a fresh persisted heartbeat enables the processing pulse.
            page.goto(base + "/job/1e7a6b33-0005-4a8b-9c1d-555555555555", wait_until="networkidle")
            queued = page.evaluate("""() => { const s=document.querySelector('[data-pending-state]'); const l=s&&s.querySelector('.pend__logo');
                return s && s.dataset.pendingState === 'queued' && l.complete && l.naturalWidth === 180 &&
                  !l.getAnimations().length && s.textContent.includes('waiting for a worker'); }""")
            results.append(_ok("queued job is distinct and does not pulse", bool(queued)))
            page.goto(base + "/job/9d3c1a7e-0001-4a8b-9c1d-111111111111", wait_until="networkidle")
            running = page.evaluate("""() => { const s=document.querySelector('[data-pending-state]'); const l=s&&s.querySelector('.pend__logo');
                return s && s.dataset.pendingState === 'processing' && l.complete && l.naturalWidth === 180 &&
                  l.getAnimations().some(a=>a.animationName==='pend-beat') && document.querySelector('.job-sheet').getAttribute('aria-busy')==='true'; }""")
            results.append(_ok("fresh heartbeat confirms processing and enables pulse", bool(running)))
            page.goto(base + "/job/2f8b7c44-0006-4a8b-9c1d-666666666666", wait_until="networkidle")
            stale = page.evaluate("""() => { const s=document.querySelector('[data-pending-state]'); const l=s&&s.querySelector('.pend__logo');
                return s && s.dataset.pendingState === 'stale' && l.complete && l.naturalWidth === 180 &&
                  !l.getAnimations().length && s.textContent.includes('not confirmed'); }""")
            results.append(_ok("stale worker state is explicit and does not pulse", bool(stale)))
            page.goto(base + "/job/3c8d5e22-0004-4a8b-9c1d-444444444444", wait_until="networkidle")
            failed = page.evaluate("!document.querySelector('[data-pending-state]') && !!document.querySelector('.band--critical')")
            results.append(_ok("failed job clears pending component and keeps error", bool(failed)))
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
            page.evaluate("CortechPending.show({now: true, label:'Opening page'})")
            page.wait_for_timeout(200)
            page.keyboard.press("Escape")
            page.wait_for_timeout(100)
            results.append(_ok("Escape clears a stuck overlay", page.evaluate("!document.querySelector('.pend')")))
            # reduced motion: pulse is off and the logo sits still
            rctx = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
            rctx.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": token, "url": f"http://127.0.0.1:{PORT}"}])
            rp = rctx.new_page()
            rp.goto(base + "/", wait_until="networkidle")
            rp.evaluate("CortechPending.show({now: true, label:'Opening page'})")
            rp.wait_for_timeout(300)
            rm = rp.evaluate("""() => { const l = document.querySelector('.pend__logo'); const h = document.querySelector('.pend__halo');
                return [getComputedStyle(l).animationName, getComputedStyle(l).transform, getComputedStyle(l).opacity,
                        getComputedStyle(h).animationName]; }""")
            results.append(_ok("reduced motion leaves static logo and status text",
                               rm[0] == "none" and rm[1] in ("none", "matrix(1, 0, 0, 1, 0, 0)") and rm[2] == "1" and rm[3] == "none", str(rm)))
            rctx.close()

            # A scrollable dense table is contained within its wrapper; the page
            # itself must never acquire accidental horizontal overflow.
            mctx = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
            mctx.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": token, "url": f"http://127.0.0.1:{PORT}"}])
            mp = mctx.new_page()
            for label, path in (("Pipeline", "/"), ("Sources", "/sources"), ("Library", "/library"),
                                ("Proposal review", "/opportunity?url=https%3A%2F%2Ftenders.example.org%2Fgiz-climate-resilience-evaluation")):
                mp.goto(base + path, wait_until="networkidle")
                widths = mp.evaluate("[document.documentElement.scrollWidth, document.body.scrollWidth, innerWidth]")
                contained = max(widths[0], widths[1]) <= widths[2] + 1
                results.append(_ok(f"390px {label} page has no horizontal overflow", contained, str(widths)))
            mctx.close()

            if "--video" in sys.argv:
                import shutil
                vctx = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="no-preference",
                                           record_video_dir=str(out), record_video_size={"width": 1440, "height": 900})
                vctx.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": token, "url": f"http://127.0.0.1:{PORT}"}])
                vp = vctx.new_page()
                vp.goto(base + "/", wait_until="networkidle")
                vp.evaluate("CortechPending.show({now:true, label:'Opening page'})")
                vp.wait_for_timeout(2400)
                vp.screenshot(path=str(out / "pending-double-beat-recording-frame.png"))
                vp.close()
                source_video = vp.video.path()
                vctx.close()
                shutil.copy2(source_video, out / "pending-double-beat.webm")
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
