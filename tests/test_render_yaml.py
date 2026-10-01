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


START_SH = Path("dashboard/start.sh")


def test_web_service_binds_port_and_has_a_health_check():
    web = _by_type("web")
    assert web["name"] == "cortech-bd-dashboard"
    assert web["dockerCommand"].strip() == "dashboard/start.sh"
    script = START_SH.read_text()
    assert "uvicorn dashboard.app:app" in script
    assert "--host 0.0.0.0" in script
    assert "PORT" in script, "the web start script must bind Render's $PORT"
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


def test_pipeline_credentials_live_on_the_worker_only():
    """The web process reads/writes Supabase only. Verified 2026-09-21 by booting
    it with no ANTHROPIC/OPENAI/AIRTABLE vars: /healthz 200, UI redirects to
    login. config.require_env() is called by the worker and main.py, never by
    dashboard.app. The internet-facing service must not hold model/CRM keys."""
    keys = {s["name"]: {e["key"] for e in s["envVars"]} for s in SPEC["services"]}
    web, worker = keys["cortech-bd-dashboard"], keys["cortech-bd-worker"]
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY",
              "DASHBOARD_AGGREGATE_CAP_USD", "DASHBOARD_AGGREGATE_WINDOW_HOURS"):
        assert k in web and k in worker, k
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AIRTABLE_API_KEY", "AIRTABLE_BASE_ID",
              "MAX_RUN_COST_USD", "EMAIL_RECIPIENTS"):
        assert k in worker and k not in web, k


def test_aggregate_cap_mirror_is_identical_on_both_services():
    vals = {s["name"]: {e["key"]: e.get("value") for e in s["envVars"]} for s in SPEC["services"]}
    for k in ("DASHBOARD_AGGREGATE_CAP_USD", "DASHBOARD_AGGREGATE_WINDOW_HOURS", "DASHBOARD_HEARTBEAT_SECONDS"):
        assert vals["cortech-bd-dashboard"][k] == vals["cortech-bd-worker"][k], k


def test_no_discovery_scheduler_vars_on_either_service():
    for s in SPEC["services"]:
        for e in s["envVars"]:
            k = e["key"]
            assert not k.startswith("IMAP_"), f"{s['name']}:{k} — newsletter source is discovery-only"
            assert k not in ("CHECK_INTERVAL_HOURS", "MAX_OPPORTUNITIES_PER_RUN", "HEALTHCHECK_URL"), \
                f"{s['name']}:{k} is a scheduler setting; neither Render service runs discovery"


def test_every_env_var_has_exactly_one_value_source():
    """A bare `- key: X` is a Blueprint spec error. PORT must not be declared:
    Render injects it for web services and the command reads ${PORT:-10000}."""
    sources = ("value", "sync", "generateValue", "fromService", "fromDatabase")
    for s in SPEC["services"]:
        for e in s["envVars"]:
            assert e["key"] != "PORT", "PORT is injected by Render; do not declare it"
            present = [f for f in sources if f in e]
            assert len(present) == 1, f"{s['name']}:{e['key']} has value sources {present}"
            if "sync" in e:
                assert e["sync"] is False, f"{s['name']}:{e['key']}: sync must be false (secret set in dashboard)"


def test_numeric_settings_have_numeric_string_defaults():
    numeric_suffixes = ("_USD", "_HOURS", "_SECONDS", "_MS", "_MB", "_BYTES", "_MAX",
                        "_RETRIES", "_REDIRECTS", "_FILES", "_CHARS", "_AGE")
    for s in SPEC["services"]:
        for e in s["envVars"]:
            if e["key"].endswith(numeric_suffixes) and "value" in e:
                assert isinstance(e["value"], str), f"{e['key']}: quote numeric values"
                float(e["value"])  # raises if not numeric


def test_every_declared_env_var_is_actually_read_by_the_code():
    """Catches a Blueprint key the code never looks at (e.g. GOOGLE_OAUTH_CLIENT_ID
    vs the real GOOGLE_CLIENT_ID). A misnamed secret is silently ignored at
    runtime; here it fails."""
    import re
    read_from = "".join(
        Path(f).read_text()
        for f in ("config.py", "dashboard/settings.py", "dashboard/worker.py",
                  "reporting/email_report.py")
    )
    literals = set(re.findall(r'"([A-Z][A-Z0-9_]{3,})"', read_from))
    for s in SPEC["services"]:
        for e in s["envVars"]:
            assert e["key"] in literals, f"{s['name']}:{e['key']} is not read anywhere in the code"


def test_no_docker_command_contains_shell_quoting():
    """2026-09-22: an inline `sh -c '...'` dockerCommand reached the container
    as ONE literal program name → exit 127 "not found". Anything that needs
    shell expansion belongs in a committed script, never in this YAML."""
    for s in SPEC["services"]:
        cmd = s["dockerCommand"]
        assert "sh -c" not in cmd, f"{s['name']}: no inline shell in dockerCommand"
        for ch in "'\"$;|&":
            assert ch not in cmd, f"{s['name']}: {ch!r} in dockerCommand — move it into a script"


def test_web_start_script_is_the_verified_command_and_executable():
    import stat
    import subprocess
    script = START_SH.read_text()
    assert script.startswith("#!/bin/sh\n"), "must run under the image's /bin/sh"
    assert "set -e" in script
    assert "exec uvicorn dashboard.app:app" in script, "exec so uvicorn is PID 1 and gets SIGTERM"
    assert '--port "${PORT:-10000}"' in script
    assert "--proxy-headers" in script, "Render terminates TLS; app must trust X-Forwarded-*"
    assert '--forwarded-allow-ips="*"' in script
    assert START_SH.stat().st_mode & stat.S_IXUSR, "dashboard/start.sh must be committed executable"
    assert subprocess.run(["sh", "-n", str(START_SH)]).returncode == 0, "start.sh has a shell syntax error"
    # The Dockerfile must not rely on the host's mode bits surviving transport.
    dockerfile = Path("Dockerfile").read_text()
    assert "RUN chmod +x dashboard/start.sh" in dockerfile
    assert not any(l.strip().startswith("dashboard/start.sh") for l in Path(".dockerignore").read_text().splitlines())


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
