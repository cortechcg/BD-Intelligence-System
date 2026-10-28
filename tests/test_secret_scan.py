"""scripts/secret_scan.py finds live credentials and never prints them.

Fake credentials are assembled at runtime so this file itself contains no
string a scanner (ours or gitleaks) would report.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import secret_scan  # noqa: E402

FAKE = {
    "anthropic_api_key": "sk-" + "ant-api03-" + ("Zq8" * 20),
    "openai_api_key": "sk-" + "proj-" + ("Xy7w" * 15),
    "airtable_pat": "pat" + "Qw3Er5Ty7Ui9Op" + "." + ("a1b2c3d4" * 8),
    "supabase_jwt": "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + ("yb2xlIjoic2Vydmlj" * 2) + "." + ("Sig_9-" * 6),
    "resend_api_key": "re" + "_Ab12Cd34_" + ("Ef56Gh78" * 3),
    "google_oauth_secret": "GOC" + "SPX-" + ("k9L8m7N6" * 3),
}


@pytest.mark.parametrize("rule,value", sorted(FAKE.items()))
def test_each_provider_format_is_found(rule, value):
    findings = secret_scan.scan_text(f"X={value}\n", "cfg.env")
    assert rule in {f.rule for f in findings}


def test_value_is_never_rendered():
    value = FAKE["airtable_pat"]
    findings = secret_scan.scan_text(f"AIRTABLE_API_KEY={value}\n", ".env")
    assert findings
    for finding in findings:
        assert value not in finding.render()
        assert value[:12] not in finding.render()
        assert finding.fingerprint.startswith("sha256:")


def test_password_assignment_is_found():
    secret = "q" + "Tz8" * 4 + "!x"
    rules = {f.rule for f in secret_scan.scan_text(f"IMAP_PASSWORD={secret}\n", ".env")}
    assert "assignment:IMAP_PASSWORD" in rules


def test_postgres_url_password_is_found():
    url = "postgresql://postgres.ref:" + "S3cr" + "etPw9" + "@aws-0.pooler.supabase.com:5432/postgres"
    rules = {f.rule for f in secret_scan.scan_text(f"DATABASE_URL={url}\n", ".env")}
    assert "postgres_url_password" in rules


@pytest.mark.parametrize("line", [
    "ANTHROPIC_API_KEY=sk-ant-api03-your-key-here",
    "SUPABASE_SERVICE_KEY=your-service-role-key",
    "GMAIL_APP_PASSWORD=your-16-char-app-password",
    "OPENAI_API_KEY = get_openai_api_key()",
    "GOOGLE_CLIENT_SECRET = settings.google_client_secret",
    "# DATABASE_URL=postgresql://postgres.your-ref:[password]@host:5432/postgres",
    "RESEND_API_KEY=",
])
def test_placeholders_and_code_are_not_findings(line):
    assert secret_scan.scan_text(line + "\n", "x") == []


def test_allow_comment_suppresses_a_deliberate_fake():
    line = f"KEY={FAKE['openai_api_key']}  # secret-scan: allow\n"
    assert secret_scan.scan_text(line, "x") == []


def test_working_tree_is_clean():
    """The committed tree carries no credential (the CI gate in miniature)."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "secret_scan.py"), "--tree"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout


def test_env_files_are_ignored():
    for name in (".env", ".env.local", ".env.bak-1"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", name], cwd=ROOT, capture_output=True
        )
        assert result.returncode == 0, f"{name} is not ignored"
    tracked = subprocess.run(
        ["git", "ls-files", ".env"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    assert tracked == ""
