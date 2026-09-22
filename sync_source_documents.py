"""Sync the source-document corpus with Supabase Storage.

``data/proposals/`` and ``data/cvs/`` are real business documents — client
financial proposals and named consultants' CVs. They are NOT tracked in git
(decision 2026-09-22, README §"Source documents"): they live in the private
Supabase Storage bucket ``Cortech-documents`` in the same project the
pipeline already uses, under ``proposals/`` and ``cvs/``.

    python sync_source_documents.py status   # what differs between local and bucket
    python sync_source_documents.py pull     # bucket → data/  (fresh clone)
    python sync_source_documents.py push     # data/  → bucket (after adding documents)

Credentials: SUPABASE_URL and SUPABASE_SERVICE_KEY from .env, exactly as the
pipeline reads them. Nothing here prints a key. Files are compared by size;
``--force`` re-transfers everything. The placeholder ``PUT_*_HERE.txt`` files
stay in git and are never uploaded.
"""
from __future__ import annotations

import argparse
import mimetypes
import sys
from pathlib import Path

import config  # noqa: F401  (loads .env)
from database.supabase_client import get_supabase

BUCKET = "Cortech-documents"
FOLDERS = {"proposals": Path("data/proposals"), "cvs": Path("data/cvs")}
PLACEHOLDER_PREFIX = "PUT_"


def _local(folder: Path) -> dict[str, int]:
    if not folder.is_dir():
        return {}
    return {
        p.name: p.stat().st_size
        for p in sorted(folder.iterdir())
        if p.is_file() and not p.name.startswith(PLACEHOLDER_PREFIX) and not p.name.startswith(".")
        and not p.name.startswith("~$")   # Office lock files: 162-byte junk, never documents
    }


def _remote(bucket, prefix: str) -> dict[str, int]:
    out: dict[str, int] = {}
    offset = 0
    while True:
        page = bucket.list(prefix, {"limit": 1000, "offset": offset, "sortBy": {"column": "name", "order": "asc"}})
        if not page:
            break
        for e in page:
            if e.get("id") is None:  # a sub-folder; the corpus is flat
                continue
            out[e["name"]] = int((e.get("metadata") or {}).get("size") or 0)
        if len(page) < 1000:
            break
        offset += len(page)
    return out


def cmd_status(bucket) -> int:
    drift = 0
    for prefix, folder in FOLDERS.items():
        local, remote = _local(folder), _remote(bucket, prefix)
        only_local = sorted(set(local) - set(remote))
        only_remote = sorted(set(remote) - set(local))
        differ = sorted(n for n in set(local) & set(remote) if local[n] != remote[n])
        print(f"{prefix}: {len(local)} local, {len(remote)} in bucket, "
              f"{len(only_local)} only local, {len(only_remote)} only in bucket, {len(differ)} differ")
        for n in only_local[:10]:
            print(f"   local only : {n}")
        for n in only_remote[:10]:
            print(f"   bucket only: {n}")
        for n in differ[:10]:
            print(f"   size differs: {n} ({local[n]} vs {remote[n]} bytes)")
        drift += len(only_local) + len(only_remote) + len(differ)
    return 1 if drift else 0


def cmd_push(bucket, force: bool) -> int:
    failed = 0
    for prefix, folder in FOLDERS.items():
        local, remote = _local(folder), _remote(bucket, prefix)
        todo = [n for n in local if force or remote.get(n) != local[n]]
        print(f"{prefix}: {len(todo)} of {len(local)} files to upload")
        for i, name in enumerate(todo, 1):
            path = folder / name
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            try:
                bucket.upload(f"{prefix}/{name}", path.read_bytes(), {"content-type": ctype, "upsert": "true"})
                print(f"   [{i}/{len(todo)}] up {name} ({path.stat().st_size // 1024} KiB)")
            except Exception as exc:  # report and continue; the summary is the contract
                failed += 1
                print(f"   [{i}/{len(todo)}] FAILED {name}: {str(exc)[:160]}")
    after = cmd_status(bucket)
    return 1 if failed or after else 0


def cmd_pull(bucket, force: bool) -> int:
    failed = 0
    for prefix, folder in FOLDERS.items():
        folder.mkdir(parents=True, exist_ok=True)
        local, remote = _local(folder), _remote(bucket, prefix)
        todo = [n for n in remote if force or local.get(n) != remote[n]]
        print(f"{prefix}: {len(todo)} of {len(remote)} files to download")
        for i, name in enumerate(todo, 1):
            try:
                data = bucket.download(f"{prefix}/{name}")
                (folder / name).write_bytes(data)
                print(f"   [{i}/{len(todo)}] down {name} ({len(data) // 1024} KiB)")
            except Exception as exc:
                failed += 1
                print(f"   [{i}/{len(todo)}] FAILED {name}: {str(exc)[:160]}")
    after = cmd_status(bucket)
    return 1 if failed or after else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("status", "pull", "push"))
    ap.add_argument("--force", action="store_true", help="transfer every file, not only size mismatches")
    args = ap.parse_args()
    sb = get_supabase()
    names = {b.name for b in sb.storage.list_buckets()}
    if BUCKET not in names:
        print(f"bucket {BUCKET!r} does not exist in this Supabase project; create it (private) in the dashboard first")
        return 2
    bucket = sb.storage.from_(BUCKET)
    if args.command == "status":
        return cmd_status(bucket)
    if args.command == "push":
        return cmd_push(bucket, args.force)
    return cmd_pull(bucket, args.force)


if __name__ == "__main__":
    sys.exit(main())
