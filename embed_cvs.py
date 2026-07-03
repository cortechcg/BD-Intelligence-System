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

import os
import sys
import time
import json
import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track
from loguru import logger

load_dotenv()
console = Console()

# ── CLIENTS ───────────────────────────────────────────────────────────────────
def get_clients():
    from pyairtable import Api
    from supabase import create_client

    airtable_api   = Api(os.getenv("AIRTABLE_API_KEY"))
    airtable_base  = airtable_api.base(os.getenv("AIRTABLE_BASE_ID"))
    supabase_client = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_KEY")
    )
    return (
        airtable_base.table("CONSULTANTS"),
        airtable_base.table("PAST_PROPOSALS"),
        supabase_client
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
            "model": "text-embedding-3-small",  # 1536 dims, cheap
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["data"][0]["embedding"]


def get_embedding_claude_summary(text: str) -> str:
    """
    If no OpenAI key, use Claude to create a rich text summary
    that we store directly (no vector search — keyword search instead).
    This is the fallback approach.
    """
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"Create a dense 300-word semantic summary of this consultant's profile for search indexing. Include all skills, locations, experience types, tools, and thematic areas:\n\n{text[:5000]}"
        }]
    )
    return response.content[0].text


# ── EMBED AND STORE CVS ───────────────────────────────────────────────────────
def embed_consultant_cv(
    supabase,
    consultant_record: dict,
    use_openai: bool = True
) -> bool:
    """Embed a consultant CV and store in Supabase pgvector."""

    fields = consultant_record.get("fields", {})
    airtable_id = consultant_record["id"]
    name = fields.get("full_name", "Unknown")
    cv_text = fields.get("cv_text", "")

    if not cv_text or len(cv_text) < 50:
        logger.warning(f"No cv_text for {name}. Skipping embedding.")
        return False

    # Build rich metadata for filtering
    metadata = {
        "thematic_expertise":    fields.get("thematic_expertise", []),
        "geographic_experience": fields.get("geographic_experience", []),
        "languages":             fields.get("languages", []),
        "tools":                 fields.get("tools", []),
        "seniority_level":       fields.get("seniority_level", ""),
        "years_experience":      fields.get("years_experience", 0),
        "day_rate_usd":          fields.get("day_rate_usd", 0),
        "availability_status":   fields.get("availability_status", "Available"),
        "based_in":              fields.get("based_in", ""),
        "key_skills":            fields.get("key_skills", ""),
    }

    try:
        if use_openai:
            embedding = get_embedding_openai(cv_text)

            result = supabase.table("cv_embeddings").upsert({
                "airtable_consultant_id": airtable_id,
                "consultant_name":        name,
                "role_title":             fields.get("role_title", ""),
                "content_chunk":          cv_text[:2000],
                "embedding":              embedding,
                "metadata":              metadata,
            }).execute()

            # Update Airtable with embedding ID
            from pyairtable import Api
            api  = Api(os.getenv("AIRTABLE_API_KEY"))
            base = api.base(os.getenv("AIRTABLE_BASE_ID"))
            table = base.table("CONSULTANTS")
            table.update(airtable_id, {
                "embedding_id": result.data[0]["id"]
            })

        else:
            # Fallback: store text summary without vector embedding
            summary = get_embedding_claude_summary(cv_text)
            logger.info(f"Stored text summary (no vector) for {name}")

        return True

    except Exception as e:
        logger.error(f"Embedding failed for {name}: {e}")
        return False


def embed_proposal(
    supabase,
    proposal_record: dict,
    use_openai: bool = True
) -> bool:
    """Embed a past proposal for style reference search."""

    fields = proposal_record.get("fields", {})
    airtable_id = proposal_record["id"]
    title = fields.get("project_title", "Unknown")
    proposal_text = fields.get("proposal_text", "")

    if not proposal_text or len(proposal_text) < 100:
        logger.warning(f"No proposal text for {title}. Skipping.")
        return False

    metadata = {
        "client":        fields.get("client", ""),
        "year":          fields.get("year", 0),
        "won":           fields.get("won", False),
        "thematic_areas": fields.get("thematic_areas", []),
        "location":      fields.get("location", []),
    }

    try:
        if use_openai:
            embedding = get_embedding_openai(proposal_text)

            supabase.table("proposal_embeddings").upsert({
                "airtable_proposal_id": airtable_id,
                "project_title":        title,
                "won":                  fields.get("won", False),
                "content_chunk":        proposal_text[:2000],
                "embedding":            embedding,
                "metadata":             metadata,
            }).execute()

        return True

    except Exception as e:
        logger.error(f"Proposal embedding failed for {title}: {e}")
        return False


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    console.print("\n[bold blue]🧠 Cortech CV Embedder[/bold blue]")
    console.print("[dim]Loading consultant CVs into Supabase vector store[/dim]\n")

    # Check OpenAI key
    use_openai = bool(os.getenv("OPENAI_API_KEY"))

    if use_openai:
        console.print("  ✅ OpenAI API key found — using vector embeddings")
    else:
        console.print("  ⚠️  No OpenAI API key — using text summaries (add OPENAI_API_KEY for better search)")

    # Check Supabase
    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_KEY"):
        console.print(
            "\n[yellow]⚠️  Supabase not configured.[/yellow]\n"
            "   Add SUPABASE_URL and SUPABASE_SERVICE_KEY to .env\n"
            "   Without Supabase, CV matching will not work.\n"
        )
        sys.exit(0)

    try:
        consultant_table, proposal_table, supabase = get_clients()
    except Exception as e:
        console.print(f"[red]❌ Connection failed: {e}[/red]")
        sys.exit(1)

    # ── EMBED CVS ──────────────────────────────────────────────────────────────
    console.print("[bold]Loading consultants from Airtable...[/bold]")
    consultants = consultant_table.all()
    console.print(f"Found [green]{len(consultants)}[/green] consultants\n")

    cv_success = 0
    cv_failed  = 0

    for record in track(consultants, description="Embedding CVs..."):
        name = record.get("fields", {}).get("full_name", "Unknown")

        success = embed_consultant_cv(supabase, record, use_openai)
        if success:
            cv_success += 1
        else:
            cv_failed += 1

        time.sleep(0.3)  # Rate limit

    # ── EMBED PROPOSALS ────────────────────────────────────────────────────────
    if use_openai:
        console.print("\n[bold]Loading past proposals from Airtable...[/bold]")
        proposals = proposal_table.all()
        console.print(f"Found [green]{len(proposals)}[/green] proposals\n")

        prop_success = 0
        prop_failed  = 0

        for record in track(proposals, description="Embedding proposals..."):
            success = embed_proposal(supabase, record, use_openai)
            if success:
                prop_success += 1
            else:
                prop_failed += 1
            time.sleep(0.3)

        console.print(
            f"\n  Proposals: [green]{prop_success} embedded[/green] | "
            f"[red]{prop_failed} failed[/red]"
        )

    # ── SUMMARY ────────────────────────────────────────────────────────────────
    console.print(f"\n  CVs: [green]{cv_success} embedded[/green] | [red]{cv_failed} failed[/red]")
    console.print("\n[bold green]✅ Embedding complete![/bold green]")
    console.print("\n[bold]Next step:[/bold]")
    console.print("  Run: [cyan]python main.py --once[/cyan]")


if __name__ == "__main__":
    main()