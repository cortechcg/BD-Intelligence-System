"""Which worker consumes which queue scope, and test URLs never reach production.

The database half (atomic claims, CHECK constraint, scope filter) is proven
against real Postgres in tests/test_queue_scopes_pg.py.
"""
import pytest

from dashboard import triggers, worker

RENDER = {"RENDER": "true", "RENDER_SERVICE_ID": "srv-x"}


@pytest.mark.parametrize("env,scope", [
    ({**RENDER, "RENDER_SERVICE_NAME": "cortech-bd-worker"}, "production"),
    ({**RENDER, "RENDER_SERVICE_NAME": "cortech-bd-worker", "DASHBOARD_WORKER_SCOPE": "production"}, "production"),
    ({**RENDER, "RENDER_SERVICE_NAME": "cortech-bd-worker", "DASHBOARD_WORKER_SCOPE": "staging"}, "staging"),
    # The Oregon copy, with or without a production claim in its env, idles.
    ({**RENDER, "RENDER_SERVICE_NAME": "BD-Intelligence-System"}, None),
    ({**RENDER, "RENDER_SERVICE_NAME": "BD-Intelligence-System", "DASHBOARD_WORKER_SCOPE": "production"}, None),
    ({**RENDER, "RENDER_SERVICE_NAME": "BD-Intelligence-System", "DASHBOARD_WORKER_SCOPE": "staging"}, "staging"),
    ({**RENDER}, None),
    ({}, None),
    ({"DASHBOARD_WORKER_SCOPE": "test"}, "test"),
    ({"DASHBOARD_WORKER_SCOPE": "production"}, None),
    ({"DASHBOARD_WORKER_SCOPE": "production", "DASHBOARD_WORKER_ALLOW_LOCAL_PRODUCTION": "1"}, "production"),
    ({**RENDER, "RENDER_SERVICE_NAME": "cortech-bd-worker", "DASHBOARD_WORKER_SCOPE": "all"}, None),
])
def test_resolve_worker_scope(env, scope):
    resolved, why = worker.resolve_worker_scope(env)
    assert resolved == scope, why
    assert why


@pytest.mark.parametrize("url,is_test", [
    ("https://dashboard-queue.test.invalid/53657695", True),
    ("https://a.test/x", True),
    ("http://localhost:8000/x", True),
    ("https://foo.example./x", True),
    ("https://upload.cortechconsultinggroup.com/tor/a19fe188bf1f4f0b/ToR.pdf", False),
    ("https://example.org/tender", False),
    ("https://testing.org/x", False),
    ("library://5f9c", False),
    ("", False),
])
def test_is_test_url(url, is_test):
    assert triggers.is_test_url(url) is is_test


def test_enqueue_files_a_test_host_under_test_even_if_asked_for_production(monkeypatch):
    sent = []
    monkeypatch.setattr(triggers, "_rpc", lambda name, params: sent.append(params) or "id-1")
    triggers.enqueue("https://dashboard-queue.test.invalid/abc", triggers.SUBMIT_URL, "ci")
    triggers.enqueue("https://example.org/tender", triggers.SUBMIT_URL, "ci")
    assert [p["p_scope"] for p in sent] == ["test", "production"]


def test_claim_requires_a_known_scope(monkeypatch):
    monkeypatch.setattr(triggers, "_rpc", lambda *a: pytest.fail("must not reach the database"))
    with pytest.raises(TypeError):
        triggers.claim("tok", 60)  # scope is mandatory
    with pytest.raises(ValueError):
        triggers.claim("tok", 60, "everything")


def test_claim_rejects_a_row_from_another_scope(monkeypatch):
    monkeypatch.setattr(triggers, "_rpc", lambda name, params: [{
        "id": "1", "source_url": "https://x.org", "trigger_kind": "submit_url",
        "requested_by": "a", "attempt_count": 1, "worker_token": "tok", "scope": "staging",
    }])
    assert triggers.claim("tok", 60, "production") is None


def _job(url):
    return {"id": "t1", "source_url": url, "trigger_kind": "submit_url",
            "requested_by": "ci", "attempt_count": 1, "worker_token": "tok", "scope": "production"}


def test_production_worker_refuses_a_test_url_without_running_the_pipeline(monkeypatch):
    finished = []
    monkeypatch.setattr(triggers, "claim", lambda *a: _job("https://dashboard-queue.test.invalid/x"))
    monkeypatch.setattr(triggers, "finish", lambda *a, **k: finished.append(k) or True)
    monkeypatch.setattr(triggers, "aggregate_cap_reached", lambda: pytest.fail("must stop before the cap check"))
    monkeypatch.setattr(worker, "run_trigger", lambda job: pytest.fail("pipeline must not run"))
    assert worker.run_once("tok", "production") is True
    assert finished[0]["status"] == "failed"
    assert finished[0]["result"] == {"gate": "test_url"}


def test_worker_passes_its_scope_to_claim_and_staging_skips_final_grades(monkeypatch):
    seen = []
    monkeypatch.setattr(triggers, "claim", lambda token, lease, scope: seen.append(scope))
    monkeypatch.setattr(worker, "grade_one_pending_final", lambda: pytest.fail("finals are production data"))
    assert worker.run_once("tok", "staging") is False
    assert seen == ["staging"]


def test_ui_reads_only_production_rows():
    import inspect

    for fn in (triggers.recent, triggers.for_url, triggers.in_flight_urls, triggers.in_flight_by_url):
        assert 'eq("scope", PRODUCTION)' in inspect.getsource(fn), fn.__name__
