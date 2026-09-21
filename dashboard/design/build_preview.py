#!/usr/bin/env python3
"""Phase 1 design probe: render the Queue screen from REAL Supabase rows.

Writes a standalone ``queue_preview.html`` next to this file, using the same
Jinja templates and the same stylesheet the running app uses — so what is
validated here is the actual direction, not a throwaway mockup.

It is populated from live reads (``docs/DASHBOARD_DESIGN.md`` §6): a sketch
full of invented tidy rows would validate the wrong thing and would violate
constraint 1 of the brief.

    python dashboard/design/build_preview.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape

from dashboard import queries

OUT = Path(__file__).resolve().parent / "queue_preview.html"


def main() -> int:
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "dashboard" / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("queue.html")

    queue = queries.queue_view(limit=6)

    html = template.render(
        view="queue",
        queue=queue,
        triggers=[],
        in_flight={},
        spend={"available": True, "spent_usd": 0.0, "runs": 0, "runs_unknown_cost": 0,
               "hours": 24, "cap_usd": 100.0},
        cap_blocked=False,
        cap_reason="",
        bulk_max=40,
        triggering_enabled=True,
        triggering_disabled_reason="",
        csrf_token="preview",
        poll_ms=15000,
        viewer="design-probe (not a session)",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        # file:// preview reads the stylesheet straight out of the repo
        static_base="../static",
        url_queue="#",
        url_portfolio="#",
        url_detail="#",
        url_logout="",
        url_job="#",
        url_trigger_url="#",
        url_trigger_existing="#",
        url_trigger_bulk="#",
        url_trigger_retry="#",
        url_trigger_rerun="#",
        url_trigger_cancel="#",
        cancel_copy="",
        flash="",
        flash_kind="",
    )
    OUT.write_text(html)
    counts = queue["counts"]
    print(f"wrote {OUT}")
    print(
        "real rows — ledger={ledger_rows} cache={cache_rows} halted={halted} "
        "in_flight={in_flight} terminal_skip={terminal_skip} drafted={drafted}".format(**counts)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
