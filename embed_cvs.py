"""
╔══════════════════════════════════════════════════════════════════╗
║              CORTECH CV EMBEDDER SCRIPT                          ║
║                                                                  ║
║  Run this AFTER populate_airtable.py                             ║
║  Takes all consultant CVs from Airtable and loads them into      ║
║  Supabase vector store for semantic search matching              ║
║                                                                  ║
║  Run: python embed_cvs.py                                        ║
╚══════════════════════════════════════════════════════════════════╝
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track
from loguru import logger

load_dotenv()
console = Console()

EMBEDDING_MODEL = "text-embedding-3-small"
MIN_CV_TEXT_CHARS = 50
MIN_PROPOSAL_TEXT_CHARS = 100


def text_field(fields: dict, candidates: list[str], default: str = "") -> str:
    """Return the first populated Airtable field from possible field names."""
    for key in candidates:
        value = fields.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            return value
    return default


def list_field(fields: dict, key: str) -> list:
    value = fields.get(key, [])
    if isinstance(value, list):
        return value
    return [value] if value else []


def table_count(supabase, table_name: str) -> int:
    result = supabase.table(table_name).select("id", count="exact").limit(1).execute()
    return result.count if result.count is not None else len(result.data or [])


def store_single_embedding_row(
    supabase,
    table_name: str,
    lookup_column: str,
    lookup_value: str,
    payload: dict,
    preferred_id: str = "",
) -> tuple[str, str, int]:
    """
    Store exactly one embedding row for a source Airtable record.

    Supabase upsert only deduplicates when the database has a matching unique
    constraint. This project table currently allows duplicates, so we repair by
    updating one row and deleting stale duplicates for the same Airtable ID.
    """
    existing = supabase.table(table_name).select("id").eq(
        lookup_column, lookup_value
    ).execute()
    rows = existing.data or []

    duplicate_count = 0
    if rows:
        keep = next(
            (row for row in rows if preferred_id and row.get("id") == preferred_id),
            rows[0],
        )
        keep_id = keep["id"]
        supabase.table(table_name).update(payload).eq("id", keep_id).execute()

        duplicate_ids = [
            row["id"] for row in rows
            if row.get("id") and row.get("id") != keep_id
        ]
        duplicate_count = len(duplicate_ids)
        for duplicate_id in duplicate_ids:
            supabase.table(table_name).delete().eq("id", duplicate_id).execute()
        action = "updated"
    else:
        inserted = supabase.table(table_name).insert(payload).execute()
        keep_id = inserted.data[0]["id"] if inserted.data else ""
        action = "inserted"

    verification = supabase.table(table_name).select("id").eq(
        lookup_column, lookup_value
    ).execute()
    verified_rows = verification.data or []

    if len(verified_rows) != 1:
        raise RuntimeError(
            f"{table_name} verification failed for {lookup_value}: "
            f"expected 1 row, found {len(verified_rows)}"
        )

    verified_id = verified_rows[0]["id"]
    if keep_id and verified_id != keep_id:
        raise RuntimeError(
            f"{table_name} verification returned unexpected row for {lookup_value}"
        )

    return verified_id, action, duplicate_count

# ── CLIENTS ───────────────────────────────────────────────────────────────────
def get_supabase():
    from supabase import create_client

    return create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_KEY"),
    )


def get_clients():
    from pyairtable import Api, retry_strategy

    # Do not retry 429 — urllib3 "too many 429" is what crashed this
    # script while Airtable was already rate-limited.
    retry = retry_strategy(
        status_forcelist=(500, 502, 503, 504),
        backoff_factor=0.5,
        total=2,
    )
    airtable_api = Api(
        os.getenv("AIRTABLE_API_KEY"),
        timeout=(10, 20),
        retry_strategy=retry,
    )
    airtable_base = airtable_api.base(os.getenv("AIRTABLE_BASE_ID"))
    return (
        airtable_base.table("CONSULTANTS"),
        airtable_base.table("PAST_PROPOSALS"),
        get_supabase(),
    )


# ── EMBEDDING ─────────────────────────────────────────────────────────────────
def get_embedding_openai(text: str) -> list[float]:
    """
    Get embedding using OpenAI API.
    This produces 1536-dimension vectors compatible with our Supabase schema.
    
    NOTE: Requires OPENAI_API_KEY in .env
    Only costs ~$0.0001 per embedding — extremely cheap.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set in .env")

    response = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "input": text[:8000],  # Max input length
            "model": EMBEDDING_MODEL,  # 1536 dims, cheap
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["data"][0]["embedding"]


# ── EMBED AND STORE CVS ───────────────────────────────────────────────────────
def embed_consultant_cv(
    supabase,
    consultant_table,
    consultant_record: dict,
    use_openai: bool = True
) -> str:
    """Embed a consultant CV and store one verified row in Supabase pgvector."""

    fields = consultant_record.get("fields", {})
    airtable_id = consultant_record["id"]
    name = text_field(
        fields,
        ["full_name", "Full Name", "Name", "consultant_name"],
        default=f"Airtable record {airtable_id}",
    )
    cv_text = text_field(fields, ["cv_text", "CV Text", "cv", "resume_text"])

    if not fields:
        logger.warning(f"Blank Airtable consultant record {airtable_id}. Skipping.")
        return "skipped"

    if len(cv_text) < MIN_CV_TEXT_CHARS:
        logger.warning(
            f"No usable cv_text for {name} ({airtable_id}). Skipping embedding."
        )
        return "skipped"

    if not use_openai:
        logger.error("OPENAI_API_KEY is required to create CV vector embeddings.")
        return "failed"

    metadata = {
        "thematic_expertise": list_field(fields, "thematic_expertise"),
        "geographic_experience": list_field(fields, "geographic_experience"),
        "languages": list_field(fields, "languages"),
        "tools": list_field(fields, "tools"),
        "seniority_level": fields.get("seniority_level", ""),
        "years_experience": fields.get("years_experience", 0),
        "day_rate_usd": fields.get("day_rate_usd", 0),
        "availability_status": fields.get("availability_status", "Available"),
        "based_in": fields.get("based_in", ""),
        "key_skills": fields.get("key_skills", ""),
    }

    try:
        embedding = get_embedding_openai(cv_text)
        embedding_id, action, duplicate_count = store_single_embedding_row(
            supabase=supabase,
            table_name="cv_embeddings",
            lookup_column="airtable_consultant_id",
            lookup_value=airtable_id,
            preferred_id=fields.get("embedding_id", ""),
            payload={
                "airtable_consultant_id": airtable_id,
                "consultant_name": name,
                "role_title": fields.get("role_title", ""),
                "content_chunk": cv_text[:2000],
                "embedding": embedding,
                "metadata": metadata,
            },
        )

        try:
            consultant_table.update(airtable_id, {"embedding_id": embedding_id}, typecast=True)
        except Exception as e:
            logger.warning(
                f"Supabase row verified for {name}, but Airtable embedding_id "
                f"update failed: {e}"
            )

        if duplicate_count:
            logger.warning(
                f"Repaired {duplicate_count} duplicate CV embedding row(s) for {name}."
            )
        logger.success(f"CV embedding {action}: {name} ({embedding_id})")
        return "embedded"

    except Exception as e:
        logger.error(f"Embedding failed for {name}: {e}")
        return "failed"


def embed_proposal(
    supabase,
    proposal_record: dict,
    use_openai: bool = True
) -> str:
    """Embed a past proposal for style reference search."""

    fields = proposal_record.get("fields", {})
    airtable_id = proposal_record["id"]
    title = text_field(
        fields,
        ["project_title", "Project Title", "Name", "title"],
        default=f"Airtable record {airtable_id}",
    )
    proposal_text = text_field(fields, ["proposal_text", "Proposal Text", "text"])

    if not fields:
        logger.warning(f"Blank Airtable proposal record {airtable_id}. Skipping.")
        return "skipped"

    if len(proposal_text) < MIN_PROPOSAL_TEXT_CHARS:
        logger.warning(f"No usable proposal text for {title}. Skipping.")
        return "skipped"

    if not use_openai:
        logger.error("OPENAI_API_KEY is required to create proposal embeddings.")
        return "failed"

    metadata = {
        "client": fields.get("client", ""),
        "year": fields.get("year", 0),
        "won": fields.get("won", False),
        "thematic_areas": list_field(fields, "thematic_areas"),
        "location": list_field(fields, "location"),
    }

    try:
        embedding = get_embedding_openai(proposal_text)
        embedding_id, action, duplicate_count = store_single_embedding_row(
            supabase=supabase,
            table_name="proposal_embeddings",
            lookup_column="airtable_proposal_id",
            lookup_value=airtable_id,
            payload={
                "airtable_proposal_id": airtable_id,
                "project_title": title,
                "won": fields.get("won", False),
                "content_chunk": proposal_text[:2000],
                "embedding": embedding,
                "metadata": metadata,
            },
        )

        if duplicate_count:
            logger.warning(
                f"Repaired {duplicate_count} duplicate proposal embedding row(s) "
                f"for {title}."
            )
        logger.success(f"Proposal embedding {action}: {title} ({embedding_id})")
        return "embedded"

    except Exception as e:
        logger.error(f"Proposal embedding failed for {title}: {e}")
        return "failed"


def read_docx_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    sections = []
    for para in doc.paragraphs:
        if para.text.strip():
            sections.append(para.text.strip())
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                sections.append(" | ".join(cells))
    return "\n".join(sections)


def embed_proposal_from_file(
    supabase,
    path: Path,
    *,
    won: bool,
    title: str,
    client: str,
    year: int,
) -> str:
    """Embed a local past-proposal file. Does not call Airtable."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        proposal_text = read_docx_text(path)
    else:
        proposal_text = path.read_text(encoding="utf-8", errors="ignore")

    if len(proposal_text) < MIN_PROPOSAL_TEXT_CHARS:
        logger.warning(f"No usable proposal text in {path.name}. Skipping.")
        return "skipped"

    lookup = f"local:{path.name}"
    title = title or path.stem
    metadata = {
        "client": client,
        "year": year,
        "won": won,
        "source_file": path.name,
    }
    embedding = get_embedding_openai(proposal_text)
    embedding_id, action, duplicate_count = store_single_embedding_row(
        supabase=supabase,
        table_name="proposal_embeddings",
        lookup_column="airtable_proposal_id",
        lookup_value=lookup,
        payload={
            "airtable_proposal_id": lookup,
            "project_title": title,
            "won": won,
            "content_chunk": proposal_text[:2000],
            "embedding": embedding,
            "metadata": metadata,
        },
    )
    if duplicate_count:
        logger.warning(
            f"Repaired {duplicate_count} duplicate proposal embedding row(s) "
            f"for {title}."
        )
    logger.success(f"Proposal embedding {action}: {title} ({embedding_id})")
    console.print(
        f"  [green]{action}[/green] {title} "
        f"({table_count(supabase, 'proposal_embeddings')} proposal_embeddings rows)"
    )
    return "embedded"


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Embed CVs and past proposals into Supabase."
    )
    parser.add_argument(
        "--file",
        metavar="NAME",
        help="Embed one file from data/proposals/ (no Airtable).",
    )
    parser.add_argument(
        "--won",
        action="store_true",
        help="Mark the --file proposal as won.",
    )
    parser.add_argument(
        "--title",
        default="",
        help="project_title for --file (defaults to filename).",
    )
    parser.add_argument(
        "--client",
        default="",
        help="client metadata for --file.",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=0,
        help="year metadata for --file.",
    )
    parser.add_argument(
        "--proposals-only",
        action="store_true",
        help="Skip CVs; embed past proposals from Airtable only.",
    )
    args = parser.parse_args()

    console.print("\n[bold blue]Cortech CV Embedder[/bold blue]")

    use_openai = bool(os.getenv("OPENAI_API_KEY"))
    if not use_openai:
        console.print(
            "  OPENAI_API_KEY is required — CV matching depends on "
            "1536-dimension vector embeddings"
        )
        sys.exit(1)
    console.print("  OpenAI API key found — using vector embeddings")

    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_KEY"):
        console.print(
            "\n[yellow]Supabase not configured.[/yellow]\n"
            "   Add SUPABASE_URL and SUPABASE_SERVICE_KEY to .env\n"
        )
        sys.exit(1)

    if args.file:
        proposals_dir = Path("data/proposals")
        wanted = args.file.lower()
        matches = [
            p for p in proposals_dir.iterdir()
            if p.is_file() and (
                p.name.lower() == wanted or wanted in p.name.lower()
            )
        ]
        if not matches:
            console.print(f"[red]No file matching '{args.file}' in {proposals_dir}[/red]")
            sys.exit(1)
        path = matches[0]
        console.print(f"  Embedding from disk: [cyan]{path.name}[/cyan]")
        try:
            status = embed_proposal_from_file(
                get_supabase(),
                path,
                won=args.won,
                title=args.title,
                client=args.client,
                year=args.year or datetime.now().year,
            )
        except Exception as e:
            console.print(f"[red]Embedding failed: {e}[/red]")
            sys.exit(1)
        if status != "embedded":
            sys.exit(1)
        console.print("\n[bold green]Embedding complete![/bold green]")
        return

    try:
        consultant_table, proposal_table, supabase = get_clients()
    except Exception as e:
        console.print(f"[red]Connection failed: {e}[/red]")
        sys.exit(1)

    cv_counts = {"embedded": 0, "skipped": 0, "failed": 0}
    prop_counts = {"embedded": 0, "skipped": 0, "failed": 0}

    if not args.proposals_only:
        console.print("[bold]Loading consultants from Airtable...[/bold]")
        try:
            consultants = consultant_table.all()
        except Exception as e:
            console.print(
                f"[red]Airtable CONSULTANTS list failed: {e}[/red]\n"
                "  If this is a 429, embed one proposal from disk instead:\n"
                "  [cyan]python embed_cvs.py --file \"the-new-filename.docx\" --won[/cyan]"
            )
            sys.exit(1)
        console.print(f"Found [green]{len(consultants)}[/green] consultants\n")

        for record in track(consultants, description="Embedding CVs..."):
            status = embed_consultant_cv(
                supabase, consultant_table, record, use_openai
            )
            cv_counts[status] += 1
            time.sleep(0.3)

    console.print("\n[bold]Loading past proposals from Airtable...[/bold]")
    try:
        proposals = proposal_table.all()
    except Exception as e:
        console.print(
            f"[red]Airtable PAST_PROPOSALS list failed: {e}[/red]\n"
            "  Embed from disk instead:\n"
            "  [cyan]python embed_cvs.py --file \"the-new-filename.docx\" --won[/cyan]"
        )
        sys.exit(1)
    console.print(f"Found [green]{len(proposals)}[/green] proposals\n")

    for record in track(proposals, description="Embedding proposals..."):
        status = embed_proposal(supabase, record, use_openai)
        prop_counts[status] += 1
        time.sleep(0.3)

    console.print(
        f"\n  Proposals: [green]{prop_counts['embedded']} embedded[/green] | "
        f"[yellow]{prop_counts['skipped']} skipped[/yellow] | "
        f"[red]{prop_counts['failed']} failed[/red]"
    )
    console.print(
        f"  Supabase proposal_embeddings rows: "
        f"[cyan]{table_count(supabase, 'proposal_embeddings')}[/cyan]"
    )

    if not args.proposals_only:
        console.print(
            f"\n  CVs: [green]{cv_counts['embedded']} embedded[/green] | "
            f"[yellow]{cv_counts['skipped']} skipped[/yellow] | "
            f"[red]{cv_counts['failed']} failed[/red]"
        )
        console.print(
            f"  Supabase cv_embeddings rows: "
            f"[cyan]{table_count(supabase, 'cv_embeddings')}[/cyan]"
        )

    if cv_counts["failed"] or prop_counts["failed"]:
        console.print("\n[bold red]Embedding finished with failures.[/bold red]")
        sys.exit(1)

    console.print("\n[bold green]Embedding complete![/bold green]")
    console.print("\n[bold]Next step:[/bold]")
    console.print("  Run: [cyan]python main.py --once[/cyan]")


if __name__ == "__main__":
    main()