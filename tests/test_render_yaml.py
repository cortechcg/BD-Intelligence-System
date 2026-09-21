"""render.yaml topology guard.

Two Render misdeploys happened on 2026-09-21: (1) a hand-made Docker service
with no Start Command ran the image default, which was bare `main.py` (the
discovery scheduler); (2) the worker command was deployed under `type: web`,
which port-scans forever because the worker binds no port. Both were config
mistakes the test suite could not see. This file makes the committed
topology a tested artefact.
"""
from __future__ import annotations

from pathlib import Path

import yaml

SPEC = yaml.safe_load(Path("render.yaml").read_text())


def _by_type(kind: str) -> dict:
    return next(s for s in SPEC["services"] if s["type"] == kind)


def test_exactly_two_services_web_and_worker():
    assert [s["type"] for s in SPEC["services"]] == ["web", "worker"]


def test_web_service_binds_port_and_has_a_health_check():
    web = _by_type("web")
    assert web["name"] == "cortech-bd-dashboard"
    cmd = web["dockerCommand"]
    assert "uvicorn dashboard.app:app" in cmd
    assert "--host 0.0.0.0" in cmd
    assert "PORT" in cmd, "the web command must bind Render's $PORT"
    assert web.get("healthCheckPath") == "/healthz"


def test_worker_is_a_background_worker_with_no_port_or_health_check():
    worker = _by_type("worker")
    assert worker["name"] == "cortech-bd-worker"
    assert worker["dockerCommand"].strip() == "python -m dashboard.worker"
    assert "healthCheckPath" not in worker
    assert "PORT" not in worker["dockerCommand"]


def test_no_service_runs_bare_main_py():
    for s in SPEC["services"]:
        cmd = s.get("dockerCommand", "") + s.get("startCommand", "")
        assert "main.py" not in cmd, f"{s['name']} runs main.py: {cmd!r}"


def test_both_services_build_the_same_image_and_branch():
    a, b = SPEC["services"]
    assert a["runtime"] == b["runtime"] == "docker"
    assert a["dockerfilePath"] == b["dockerfilePath"] == "./Dockerfile"
    assert a["branch"] == b["branch"] == "main"
    assert a["region"] == b["region"]


def test_google_oauth_secrets_are_on_the_web_service_only():
    keys = {s["name"]: {e["key"] for e in s["envVars"]} for s in SPEC["services"]}
    web, worker = keys["cortech-bd-dashboard"], keys["cortech-bd-worker"]
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "PUBLIC_BASE_URL", "DASHBOARD_SESSION_SECRET"):
        assert k in web and k not in worker, k


def test_shared_pipeline_env_is_declared_on_both():
    keys = {s["name"]: {e["key"] for e in s["envVars"]} for s in SPEC["services"]}
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
              "AIRTABLE_API_KEY", "AIRTABLE_BASE_ID", "DASHBOARD_AGGREGATE_CAP_USD"):
        assert k in keys["cortech-bd-dashboard"] and k in keys["cortech-bd-worker"], k
    assert "MAX_RUN_COST_USD" in keys["cortech-bd-worker"]


def test_no_secret_values_are_committed():
    for s in SPEC["services"]:
        for e in s["envVars"]:
            if e["key"].endswith(("_KEY", "_SECRET", "_PASSWORD")) or e["key"] == "SUPABASE_URL":
                assert "value" not in e, f"{s['name']}:{e['key']} has a committed value"
                assert e.get("sync") is False or e.get("generateValue") is True


def test_deprecated_blueprint_fields_are_not_used():
    for s in SPEC["services"]:
        assert "autoDeploy" not in s, "use autoDeployTrigger (Blueprint spec deprecates autoDeploy)"
        assert "previewsEnabled" not in s


def test_dockerfile_default_is_the_worker_not_the_scheduler():
    cmd_line = next(l for l in Path("Dockerfile").read_text().splitlines() if l.startswith("CMD"))
    assert "dashboard.worker" in cmd_line and "main.py" not in cmd_line
