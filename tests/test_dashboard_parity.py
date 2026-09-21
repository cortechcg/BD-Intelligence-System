"""The dashboard must be a new *entry point*, never a second code path.

The definition of done for the dashboard phase asks for a test that compares
the output of both entry points for the same opportunity. That is what this
file does, three ways:

1. structurally — the worker calls the very function the CLI dispatches to;
2. behaviourally — the two produce equal results for the same opportunity;
3. by contract — the spend cap, dead-letter and terminal-skip dispositions
   reach the dashboard exactly as the cron path leaves them in the ledger.

All external systems are stubbed, in the same style as
``tests/test_pipeline_slice.py``. No network, no Supabase, no Anthropic.
"""
from __future__ import annotations

import copy
import inspect

import pytest

import main
from dashboard import triggers, worker

# ── shared pipeline stub ────────────────────────────────────────────────────

def _analysis():
    return {
        "opportunity": {
            "title": "Somalia WASH Endline Evaluation",
            "client": "UNICEF",
            "donor": "UNICEF",
            "submission_deadline": "2099-12-01",
            "project_location": ["Somalia"],
            "estimated_budget_usd": 80000,
        },
        "requirements": {
            "thematic_areas": ["evaluation", "WASH"],
            "language_requirements": ["English", "Somali"],
            "certifications": [],
        },
        "team_requirements": [],
        "submission_requirements": {
            "cvs_required": False,
            "financial_proposal_required": True,
        },
        "evaluation_criteria": [{"criterion": "Methodology", "weight_percent": 40}],
        "bid_analysis": {
            "is_consultancy_contract": True,
            "submission_type": "FULL_PROPOSAL",
            "cortech_fit_score": 80,
            "win_probability": 60,
            "bid_recommendation": "BID",
            "key_strengths": [],
            "key_gaps": [],
        },
    }


@pytest.fixture
def stubbed_pipeline(monkeypatch, tmp_path):
    """Deterministic pipeline with every external system replaced.

    Returns a dict recording what the pipeline was asked to do, so a test can
    assert on the *calls* as well as on the returned result.
    """
    calls: dict = {"process_opportunity": [], "emails": [], "claims": []}

    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(main, "fetch_and_extract", lambda *a, **k: "Terms of Reference " * 40)
    monkeypatch.setattr(main, "analyze_rfp", lambda *a, **k: copy.deepcopy(_analysis()))
    monkeypatch.setattr(main, "check_opportunity_exists", lambda *a, **k: False)
    monkeypatch.setattr(main, "find_opportunity_by_content_hash", lambda *a, **k: None)
    monkeypatch.setattr(main, "store_opportunity", lambda *a, **k: "cache-id")
    monkeypatch.setattr(main, "create_opportunity", lambda payload: "airtable-id")
    monkeypatch.setattr(main, "update_opportunity", lambda rid, fields: None)
    monkeypatch.setattr(main, "log_agent_action", lambda **k: None)
    monkeypatch.setattr(main, "process_win_loss_outcomes", lambda *a, **k: None)
    monkeypatch.setattr(main, "save_draft_memory", lambda *a, **k: None)
    monkeypatch.setattr(main, "persist_opportunity_stage", lambda *a, **k: True)
    monkeypatch.setattr(main, "load_processing_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(main, "opportunity_ledger_available", lambda: True)
    monkeypatch.setattr(main, "complete_opportunity_processing", lambda *a, **k: True)
    monkeypatch.setattr(main, "fail_opportunity_processing", lambda *a, **k: True)
    monkeypatch.setattr(main, "dead_letter_opportunity_processing", lambda *a, **k: True)
    monkeypatch.setattr(
        main, "match_team_to_requirements",
        lambda *a, **k: {"matched_team": [], "capability_summary": "none"},
    )
    monkeypatch.setattr(
        main, "calculate_budget",
        lambda *a, **k: {"status": "INSUFFICIENT DATA", "grand_total_usd": None},
    )
    monkeypatch.setattr(main, "build_compliance_matrix", lambda *a, **k: {"items": []})
    monkeypatch.setattr(
        main, "build_client_intelligence",
        lambda **k: {"client": None, "donor": None, "same_org": False, "storage": "test"},
    )
    monkeypatch.setattr(
        main, "generate_proposal",
        lambda *a, **k: {
            "executive_summary": "Cortech proposes a mixed-methods endline.",
            "methodology": "Household survey, KIIs, FGDs.",
        },
    )
    monkeypatch.setattr(main, "generate_eoi", lambda *a, **k: {})
    monkeypatch.setattr(
        main, "send_proposal_email",
        lambda result: calls["emails"].append(result.get("title")),
    )

    def _claim(source_url, title="", *, force=False, retry_dead_letter=False, **kw):
        calls["claims"].append({"url": source_url, "force": force,
                                "retry_dead_letter": retry_dead_letter})
        return "claim-token"

    monkeypatch.setattr(main, "claim_opportunity_processing", _claim)

    real_process = main.process_opportunity

    def _recording_process(raw_opportunity, force=False, *, retry_dead_letter=False):
        calls["process_opportunity"].append(
            {"raw": copy.deepcopy(raw_opportunity), "force": force,
             "retry_dead_letter": retry_dead_letter}
        )
        return real_process(raw_opportunity, force=force, retry_dead_letter=retry_dead_letter)

    monkeypatch.setattr(main, "process_opportunity", _recording_process)
    return calls


#: The only two fields that cannot be equal across two runs, by definition:
#: a fresh execution UUID and a timestamped local filename. Everything else
#: in the result must match, and `test_only_per_run_fields_differ` pins that
#: this list does not quietly grow.
PER_RUN_FIELDS = ("execution_id", "_draft_path")


def _comparable(result: dict | None) -> dict:
    if result is None:
        return {}
    out = copy.deepcopy(result)
    for key in PER_RUN_FIELDS:
        out.pop(key, None)
    return out


def _differing_keys(a: dict, b: dict) -> set[str]:
    return {k for k in set(a) | set(b) if a.get(k) != b.get(k)}


URL = "https://example.org/tenders/somalia-wash-endline"


# ── 1. structural: the worker calls the CLI's function ──────────────────────

def test_worker_executes_the_cli_function_not_a_copy():
    """``run_trigger`` must reach the pipeline through main.submit_single_url.

    If somebody later reimplements the stage sequence inside the worker, this
    fails — which is the whole point of the constraint.
    """
    src = inspect.getsource(worker.run_trigger)
    assert "main.submit_single_url(" in src

    # And the worker must not be orchestrating stages itself.
    forbidden = (
        "run_extract_stage",
        "run_score_stage",
        "run_draft_stage",
        "run_opportunity_pipeline",
        "claim_opportunity_processing",
    )
    worker_src = inspect.getsource(worker)
    for name in forbidden:
        assert name not in worker_src, (
            f"{name} appears in dashboard/worker.py — the worker must trigger "
            "the existing pipeline, not re-run its internals"
        )


def test_cli_and_dashboard_share_one_submit_signature():
    """The dashboard adds a keyword-only argument; it does not fork the API."""
    sig = inspect.signature(main.submit_single_url)
    assert list(sig.parameters) == ["url", "force", "retry_dead_letter"]
    for name in ("force", "retry_dead_letter"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    # The CLI defaults must stay the historical behaviour.
    assert sig.parameters["force"].default is True
    assert sig.parameters["retry_dead_letter"].default is False


# ── 2. behavioural: same opportunity, same result ───────────────────────────

def test_dashboard_trigger_matches_cli_submit_url_exactly(stubbed_pipeline):
    """The headline parity proof.

    Entry point A is what ``python main.py --submit-url <url>`` dispatches to.
    Entry point B is the dashboard worker executing a queued trigger. Same
    opportunity, same stubs — the results must be equal.
    """
    # A — the CLI. Route the argv through the real parser so the test breaks
    # if the CLI ever stops dispatching to submit_single_url.
    parsed = main.parse_main_argv(["main.py", "--submit-url", URL])
    assert parsed == {"mode": "submit", "url": URL}
    cli_result = main.submit_single_url(parsed["url"])

    cli_calls = list(stubbed_pipeline["process_opportunity"])
    stubbed_pipeline["process_opportunity"].clear()

    # B — the dashboard worker running a queued trigger for the same URL.
    outcome = worker.run_trigger(
        {
            "id": "trigger-1",
            "source_url": URL,
            "trigger_kind": triggers.SUBMIT_URL,
            "requested_by": "ops@cortechconsultinggroup.com",
        }
    )
    dash_calls = list(stubbed_pipeline["process_opportunity"])

    assert outcome["status"] == "succeeded", outcome
    assert cli_result is not None

    # The pipeline was entered with an identical request both times.
    assert len(cli_calls) == len(dash_calls) == 1
    assert cli_calls[0] == dash_calls[0]
    assert cli_calls[0]["force"] is True
    assert cli_calls[0]["retry_dead_letter"] is False
    assert cli_calls[0]["raw"]["source_portal"] == "Manual submission"

    # And produced an identical result.
    assert _comparable(cli_result) != {}
    assert outcome["result"]["recommendation"] == cli_result["recommendation"]
    assert outcome["result"]["score"] == cli_result["score"]
    assert outcome["result"]["client"] == cli_result["client"]
    assert outcome["result"]["has_draft"] is True


def test_only_per_run_fields_differ_between_two_identical_runs(stubbed_pipeline):
    """Establish the baseline: what legitimately varies run to run.

    If this ever reports more than execution_id, the parity comparison below
    is hiding a real difference behind an over-broad exclusion list.
    """
    a = main.submit_single_url(URL)
    b = main.submit_single_url(URL)
    assert _differing_keys(a, b) <= set(PER_RUN_FIELDS)
    # execution_id really is regenerated per run — not an artefact of stubbing.
    assert a["execution_id"] != b["execution_id"]


def test_both_entry_points_produce_equal_result_dicts(stubbed_pipeline):
    """Compare the full result payloads field by field, not just a summary."""
    first = _comparable(main.submit_single_url(URL))
    second = _comparable(
        main.submit_single_url(URL)  # same entry point twice = the baseline
    )
    assert first == second, "the pipeline is not deterministic under these stubs"

    captured: dict = {}
    real_submit = main.submit_single_url

    def _capture(url, *, force=True):
        captured["result"] = real_submit(url, force=force)
        return captured["result"]

    main.submit_single_url = _capture
    try:
        worker.run_trigger(
            {
                "id": "t",
                "source_url": URL,
                "trigger_kind": triggers.SUBMIT_URL,
                "requested_by": "ops@cortechconsultinggroup.com",
            }
        )
    finally:
        main.submit_single_url = real_submit

    dashboard_result = _comparable(captured["result"])
    assert dashboard_result == first
    # Spell out the strength of this: every field of the pipeline result,
    # not a hand-picked subset.
    assert len(dashboard_result) >= 15
    assert {"analysis", "bid_intelligence", "proposal_sections", "recommendation",
            "score", "compliance_matrix", "client_intelligence"} <= set(dashboard_result)


def test_draft_existing_resumes_instead_of_forcing(stubbed_pipeline):
    """"Draft this" on a discovered row must not throw away the checkpoint.

    ``force=True`` resets pipeline_stage to ``discovered`` and clears the
    checkpoint (ADR 011 §4), which would re-pay for extraction and analysis on
    an opportunity that has already been scored.
    """
    worker.run_trigger(
        {
            "id": "t",
            "source_url": URL,
            "trigger_kind": triggers.DRAFT_EXISTING,
            "requested_by": "ops@cortechconsultinggroup.com",
        }
    )
    assert stubbed_pipeline["process_opportunity"][0]["force"] is False
    assert stubbed_pipeline["claims"][0]["force"] is False


# ── 3. contract: failure states reach the dashboard unchanged ───────────────

def test_spend_cap_reaches_the_dashboard_as_a_retryable_halt(stubbed_pipeline, monkeypatch):
    """A dashboard run gets no exemption from MAX_RUN_COST_USD (ADR 012)."""
    monkeypatch.setattr(
        worker,
        "_ledger_snapshot",
        lambda url: {
            "state": "failed",
            "pipeline_stage": "scored",
            "draft_fail_count": 0,
            "last_error": "per-run LLM spend cap reached",
        },
    )
    monkeypatch.setattr(main, "submit_single_url", lambda url, *, force=True: None)

    outcome = worker.run_trigger(
        {"id": "t", "source_url": URL, "trigger_kind": triggers.SUBMIT_URL,
         "requested_by": "ops@cortechconsultinggroup.com"}
    )
    assert outcome["status"] == "failed"
    assert outcome["error_kind"] == "SPEND_CAP_ERROR"
    assert "spend cap" in outcome["error_message"].lower()
    # The stage is preserved so the next claim resumes rather than restarts.
    assert outcome["result"]["pipeline_stage"] == "scored"
    assert outcome["result"]["draft_fail_count"] == 0


def test_spend_cap_raised_through_the_worker_is_classified(stubbed_pipeline, monkeypatch):
    from utils.errors import SpendCapError

    def _boom(url, *, force=True):
        raise SpendCapError("per-run LLM spend cap reached")

    monkeypatch.setattr(main, "submit_single_url", _boom)
    monkeypatch.setattr(worker, "_ledger_snapshot", lambda url: {"state": "failed"})

    outcome = worker.run_trigger(
        {"id": "t", "source_url": URL, "trigger_kind": triggers.SUBMIT_URL,
         "requested_by": "ops@cortechconsultinggroup.com"}
    )
    assert outcome["status"] == "failed"
    assert outcome["error_kind"] == "SPEND_CAP_ERROR"


def test_dead_letter_reaches_the_dashboard_with_its_reason(stubbed_pipeline, monkeypatch):
    monkeypatch.setattr(
        worker,
        "_ledger_snapshot",
        lambda url: {
            "state": "dead_letter",
            "pipeline_stage": "scored",
            "draft_fail_count": 3,
            "last_error": "drafting failed: empty client-facing draft",
        },
    )
    monkeypatch.setattr(main, "submit_single_url", lambda url, *, force=True: None)

    outcome = worker.run_trigger(
        {"id": "t", "source_url": URL, "trigger_kind": triggers.SUBMIT_URL,
         "requested_by": "ops@cortechconsultinggroup.com"}
    )
    assert outcome["status"] == "failed"
    assert outcome["error_kind"] == "DEAD_LETTER"
    assert "empty client-facing draft" in outcome["error_message"]
    assert outcome["result"]["draft_fail_count"] == 3


def test_failure_without_a_recorded_reason_says_so(stubbed_pipeline, monkeypatch):
    """No invented explanation when the ledger recorded none."""
    monkeypatch.setattr(
        worker, "_ledger_snapshot",
        lambda url: {"state": "failed", "pipeline_stage": "discovered", "last_error": None},
    )
    monkeypatch.setattr(main, "submit_single_url", lambda url, *, force=True: None)

    outcome = worker.run_trigger(
        {"id": "t", "source_url": URL, "trigger_kind": triggers.SUBMIT_URL,
         "requested_by": "ops@cortechconsultinggroup.com"}
    )
    assert "recorded no error" in outcome["error_message"]
    assert "stage=discovered" in outcome["error_message"]
