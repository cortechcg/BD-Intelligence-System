"""Find live credentials in this repository. Prints names and fingerprints only.

    python scripts/secret_scan.py --staged    # pre-commit: what is about to be committed
    python scripts/secret_scan.py --tree      # every tracked file in the working tree
    python scripts/secret_scan.py --history   # every blob reachable from any ref

Exit 1 when anything is found. A finding is reported as rule, location and a
fingerprint (first four characters plus a sha256 prefix). The value itself is
never printed, so the scan output is safe to paste into a ticket or CI log.

No third-party dependency on purpose: the pre-commit hook must work on a fresh
clone before ``pip install`` has run. CI also runs gitleaks over full history.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass

# Provider formats. Each pattern matches the credential itself.
RULES: dict[str, re.Pattern] = {
    "anthropic_api_key": re.compile(r"sk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_\-]{40,}"),
    "openai_api_key": re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}T3BlbkFJ[A-Za-z0-9_\-]{20,}|sk-proj-[A-Za-z0-9_\-]{40,}"),
    "airtable_pat": re.compile(r"\bpat[A-Za-z0-9]{14}\.[a-f0-9]{64}\b"),
    "airtable_legacy_key": re.compile(r"\bkey[A-Za-z0-9]{14}\b(?=[\s\"']|$)"),
    "supabase_jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),
    "supabase_secret_key": re.compile(r"\bsb_secret_[A-Za-z0-9_\-]{20,}"),
    "supabase_access_token": re.compile(r"\bsbp_[a-f0-9]{40}\b"),
    "resend_api_key": re.compile(r"\bre_[A-Za-z0-9]{8,}_[A-Za-z0-9]{16,}"),
    "google_oauth_secret": re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{20,}"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "postgres_url_password": re.compile(r"postgres(?:ql)?://[^\s:/@\"']+:([^\s@\"'\[\]<>]{4,})@"),
}

# NAME=value assignments whose value must never be committed.
_SECRET_NAME = re.compile(
    r"^\s*(?:export\s+)?([A-Z0-9_]*(?:PASSWORD|SECRET|API_KEY|SERVICE_KEY|ACCESS_TOKEN|APP_PASSWORD)[A-Z0-9_]*)"
    r"\s*[=:]\s*[\"']?([^\s\"'#]+)",
    re.M,
)

_PLACEHOLDER = re.compile(
    r"your|here|example|placeholder|changeme|dummy|redacted|xxxx|\*\*\*|<|>|\$\{|\$\(|"
    r"^(?:none|null|true|false|test|secret|password|sk-\.\.\.|\.\.\.)$|"
    r"^(?:os\.|getenv|environ|settings\.|config\.)",
    re.I,
)

# Test fixtures that deliberately contain fake-but-realistic shapes.
_ALLOW_PATHS = (
    "tests/test_secret_scan.py",
)


@dataclass(frozen=True)
class Finding:
    rule: str
    location: str
    fingerprint: str

    def render(self) -> str:
        return f"{self.rule:24s} {self.location}  [{self.fingerprint}]"


def fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:10]
    return f"sha256:{digest}"


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.search(value)) or len(set(value)) <= 3


def _looks_like_code(value: str) -> bool:
    """`KEY = get_key()` or `KEY = settings.KEY` is a reference, not a credential."""
    return any(ch in value for ch in "()[]{}") or bool(re.fullmatch(r"[a-z_][a-z0-9_]*[._][a-z0-9_.]+", value))


def scan_text(text: str, location: str) -> list[Finding]:
    """All findings in one blob of text. Values are fingerprinted, never kept."""
    found: dict[tuple[str, str], Finding] = {}
    for line_no, line in enumerate(text.splitlines(), 1):
        if "secret-scan: allow" in line:
            continue
        for rule, pattern in RULES.items():
            for match in pattern.finditer(line):
                value = match.group(1) if match.groups() else match.group(0)
                if _is_placeholder(value):
                    continue
                key = (rule, fingerprint(value))
                found.setdefault(key, Finding(rule, f"{location}:{line_no}", key[1]))
        for match in _SECRET_NAME.finditer(line):
            name, value = match.group(1), match.group(2)
            if len(value) < 8 or _is_placeholder(value) or _looks_like_code(value):
                continue
            key = (f"assignment:{name}", fingerprint(value))
            found.setdefault(key, Finding(key[0], f"{location}:{line_no}", key[1]))
    return list(found.values())


def _git(*args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, input=input_bytes
    ).stdout


def _decode(blob: bytes) -> str | None:
    if b"\0" in blob[:8000]:
        return None  # binary
    return blob.decode("utf-8", "replace")


def _allowed(path: str) -> bool:
    return any(path.endswith(p) for p in _ALLOW_PATHS)


def scan_staged() -> list[Finding]:
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0")
    findings: list[Finding] = []
    for raw in names:
        path = raw.decode()
        if not path or _allowed(path):
            continue
        text = _decode(_git("show", f":{path}"))
        if text is not None:
            findings.extend(scan_text(text, path))
    return findings


def scan_tree() -> list[Finding]:
    findings: list[Finding] = []
    for raw in _git("ls-files", "-z").split(b"\0"):
        path = raw.decode()
        if not path or _allowed(path):
            continue
        try:
            with open(path, "rb") as handle:
                text = _decode(handle.read())
        except OSError:
            continue
        if text is not None:
            findings.extend(scan_text(text, path))
    return findings


def scan_history() -> list[Finding]:
    """Every blob reachable from any ref, each read once."""
    paths: dict[str, str] = {}
    for line in _git("rev-list", "--objects", "--all").decode().splitlines():
        sha, _, path = line.partition(" ")
        if path and sha not in paths:
            paths[sha] = path
    shas = [s for s, p in paths.items() if not _allowed(p)]
    if not shas:
        return []
    out = _git("cat-file", "--batch", input_bytes="\n".join(shas).encode() + b"\n")
    findings: list[Finding] = []
    pos = 0
    while pos < len(out):
        header_end = out.index(b"\n", pos)
        sha, kind, size = out[pos:header_end].decode().split()
        start = header_end + 1
        body = out[start:start + int(size)]
        pos = start + int(size) + 1
        if kind != "blob":
            continue
        text = _decode(body)
        if text is not None:
            findings.extend(scan_text(text, f"{paths[sha]}@blob:{sha[:10]}"))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--tree", action="store_true")
    mode.add_argument("--history", action="store_true")
    args = parser.parse_args(argv)
    if args.staged:
        findings = scan_staged()
    elif args.tree:
        findings = scan_tree()
    else:
        findings = scan_history()
    for finding in sorted(findings, key=lambda f: (f.location, f.rule)):
        print(finding.render())
    scope = "staged" if args.staged else "tree" if args.tree else "history"
    print(f"secret-scan ({scope}): {len(findings)} finding(s)", file=sys.stderr)
    if findings:
        print(
            "Remove the value, rotate the credential at the provider, and keep it in .env "
            "(ignored). Mark a deliberate fake with the comment 'secret-scan: allow'.",
            file=sys.stderr,
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
