#!/usr/bin/env python3
"""Apply a SQL file to the hosted Supabase Postgres for this repo.

PostgREST cannot run DDL. This script never prints credentials. It loads
connection settings from .env via the same dotenv rules as config.py.

Usage:
  python scripts/apply_sql_migration.py supabase_migration_opportunity_stages.sql
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


def _clean(name: str) -> str | None:
    raw = os.getenv(name) or ""
    return raw.strip().strip('"').strip("'").rstrip("/").strip() or None


def _load_env() -> None:
    load_dotenv(ENV_PATH, override=True, interpolate=False)


def _redact(text: str) -> str:
    secrets = [
        _clean("SUPABASE_SERVICE_KEY"),
        _clean("DATABASE_URL"),
        _clean("SUPABASE_DB_URL"),
        _clean("DIRECT_URL"),
        _clean("SUPABASE_ACCESS_TOKEN"),
        _clean("SUPABASE_DB_PASSWORD"),
        _clean("SUPABASE_URL"),
    ]
    out = text or ""
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return out[:1200]


def _project_ref() -> str | None:
    url = _clean("SUPABASE_URL") or ""
    host = urlparse(url).hostname or ""
    if not host.endswith("supabase.co"):
        return None
    return host.split(".")[0] or None


def _run_psql(db_url: str, sql_path: Path) -> int:
    env = os.environ.copy()
    # psql reads the URL; keep it out of argv logs.
    env["PGCONNECT_TIMEOUT"] = "15"
    proc = subprocess.run(
        [
            "psql",
            db_url,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            str(sql_path),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(_redact(proc.stdout or ""))
    sys.stderr.write(_redact(proc.stderr or ""))
    if proc.returncode == 0:
        print(f"APPLY_OK via=psql file={sql_path.name}")
    else:
        print(f"APPLY_FAIL via=psql exit={proc.returncode}")
    return proc.returncode


def _run_management_api(sql_path: Path) -> int:
    token = _clean("SUPABASE_ACCESS_TOKEN")
    ref = _project_ref()
    if not token or not ref:
        return 2
    sql = sql_path.read_text()
    url = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    try:
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"query": sql},
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        print(f"APPLY_FAIL via=management_api error={type(exc).__name__}")
        return 1
    body = _redact(response.text)
    print(f"APPLY management_api status={response.status_code} body={body}")
    if response.status_code >= 400:
        print("APPLY_FAIL via=management_api")
        return 1
    print(f"APPLY_OK via=management_api file={sql_path.name}")
    return 0


def _run_supabase_cli(sql_path: Path) -> int:
    token = _clean("SUPABASE_ACCESS_TOKEN")
    ref = _project_ref()
    if not token or not ref:
        return 2
    env = os.environ.copy()
    env["SUPABASE_ACCESS_TOKEN"] = token
    proc = subprocess.run(
        [
            "npx",
            "--yes",
            "supabase",
            "db",
            "query",
            "--project-ref",
            ref,
            "--file",
            str(sql_path),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(_redact(proc.stdout or ""))
    sys.stderr.write(_redact(proc.stderr or ""))
    if proc.returncode == 0:
        print(f"APPLY_OK via=supabase_cli file={sql_path.name}")
    else:
        print(f"APPLY_FAIL via=supabase_cli exit={proc.returncode}")
    return proc.returncode


def apply_sql_file(sql_path: Path) -> int:
    if not sql_path.is_file():
        print(f"APPLY_FAIL missing_file={sql_path.name}")
        return 2

    db_url = (
        _clean("DATABASE_URL")
        or _clean("SUPABASE_DB_URL")
        or _clean("DIRECT_URL")
    )
    if db_url:
        return _run_psql(db_url, sql_path)

    db_password = _clean("SUPABASE_DB_PASSWORD")
    ref = _project_ref()
    if db_password and ref:
        # Session pooler / direct hosts still need the database password.
        # Prefer the IPv4-capable pooler if the operator exported a region.
        region = _clean("SUPABASE_DB_REGION") or "eu-west-1"
        pooler = (
            f"postgresql://postgres.{ref}:{db_password}"
            f"@aws-0-{region}.pooler.supabase.com:6543/postgres"
            "?sslmode=require"
        )
        print("APPLY trying=psql_pooler (password from SUPABASE_DB_PASSWORD)")
        rc = _run_psql(pooler, sql_path)
        if rc == 0:
            return 0

    rc = _run_management_api(sql_path)
    if rc == 0:
        return 0
    if rc == 1:
        return rc

    rc = _run_supabase_cli(sql_path)
    if rc == 0:
        return 0
    if rc == 1:
        return rc

    print(
        "APPLY_FAIL reason=no_postgres_ddl_channel "
        "PostgREST cannot run ALTER/CREATE. Set DATABASE_URL or "
        "SUPABASE_DB_PASSWORD (optional SUPABASE_DB_REGION) or "
        "SUPABASE_ACCESS_TOKEN in the process environment — not by "
        "editing .env in this session — then rerun. Or paste the SQL "
        "file into the Supabase Dashboard SQL editor."
    )
    return 2


def main(argv: list[str]) -> int:
    _load_env()
    if len(argv) != 2:
        print("Usage: python scripts/apply_sql_migration.py <file.sql>")
        return 2
    return apply_sql_file((ROOT / argv[1]).resolve() if not Path(argv[1]).is_absolute() else Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
