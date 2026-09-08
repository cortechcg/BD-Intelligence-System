"""PGRST205 / missing opportunity_processing must not draft bulk discoveries."""

import pytest

import main
from database import supabase_client


PGRST205 = (
    "PGRST205: Could not find the table 'public.opportunity_processing' "
    "in the schema cache"
)


class _MissingLedger:
    def table(self, name):
        raise RuntimeError(PGRST205)

    def rpc(self, *args, **kwargs):
        raise RuntimeError(PGRST205)


@pytest.fixture(autouse=True)
def _reset_ledger():
    supabase_client.reset_opportunity_ledger_status()
    yield
    supabase_client.reset_opportunity_ledger_status()


def test_missing_opportunity_processing_does_not_draft_37_somali_jobs(monkeypatch):
    """The dangerous --once failure: every Somali Jobs URL looks new."""
    supabase_client.reset_opportunity_ledger_status()
    monkeypatch.setattr(supabase_client, "supabase", _MissingLedger())

    drafted = []
    monkeypatch.setattr(main, "RSS_FEEDS", [])
    monkeypatch.setattr(
        main,
        "scrape_non_rss_sources",
        lambda: [
            {
                "title": f"Tender {i}",
                "source_url": f"https://www.somalijobs.com/tenders/{i}/eval",
            }
            for i in range(37)
        ],
    )
    monkeypatch.setattr(main, "check_assortis_newsletter", lambda: [])
    monkeypatch.setattr(main, "send_report", lambda **kwargs: None)
    monkeypatch.setattr(main, "send_proposal_email", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "ping_healthcheck", lambda **kwargs: True)
    monkeypatch.setattr(
        main,
        "_process_opportunity_pipeline",
        lambda *args, **kwargs: drafted.append("draft")
        or {"title": "x", "_cache_text": "x"},
    )

    main._run_pipeline()
    assert drafted == []


def test_pgrst205_process_opportunity_bulk_does_not_call_pipeline(monkeypatch):
    supabase_client.reset_opportunity_ledger_status()
    monkeypatch.setattr(supabase_client, "supabase", _MissingLedger())
    called = []
    monkeypatch.setattr(
        main,
        "_process_opportunity_pipeline",
        lambda *args, **kwargs: called.append(1) or {"title": "x"},
    )
    result = main.process_opportunity({
        "title": "Somali Jobs tender",
        "source_url": "https://www.somalijobs.com/tenders/99/eval",
    })
    assert result is None
    assert called == []


def test_pgrst205_manual_force_still_reaches_pipeline(monkeypatch):
    supabase_client.reset_opportunity_ledger_status()
    monkeypatch.setattr(supabase_client, "supabase", _MissingLedger())
    monkeypatch.setattr(
        main,
        "_process_opportunity_pipeline",
        lambda *args, **kwargs: {"title": "manual", "_cache_text": "x"},
    )
    monkeypatch.setattr(main, "complete_opportunity_processing", lambda *a, **k: False)
    monkeypatch.setattr(main, "fail_opportunity_processing", lambda *a, **k: False)
    result = main.process_opportunity(
        {
            "title": "Manual URL",
            "source_url": "https://example.test/tor.pdf",
        },
        force=True,
    )
    assert result is not None
    assert result["title"] == "manual"
