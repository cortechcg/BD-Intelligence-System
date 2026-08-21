# database/supabase_client.py
import os
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_SERVICE_KEY, CLAUDE_MODEL, get_anthropic_client
from loguru import logger
import httpx
import json
from typing import Optional

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
claude = get_anthropic_client()


def get_embedding(text: str) -> list[float]:
    """
    Get text embedding using OpenAI's text-embedding-3-small.
    MUST match the model used in embed_cvs.py — both read/write the
    same vector(1536) column in Supabase, so query embeddings and
    stored CV embeddings have to come from the same model.
    """
    if len(text) > 8000:
        text = summarize_for_embedding(text)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY not set in .env — required for CV semantic "
            "search. Add it or CV matching will fail."
        )

    response = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"input": text[:8000], "model": "text-embedding-3-small"},
        timeout=30,
    )

    if response.status_code == 200:
        return response.json()["data"][0]["embedding"]

    logger.error(f"Embedding failed: {response.status_code} {response.text}")
    raise Exception(f"Failed to get embedding: {response.text}")


def summarize_for_embedding(text: str) -> str:
    """Summarize long text before embedding. Reuses the module-level client."""
    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"Summarize this CV/document in 300 words, capturing key skills, experience, geographic focus, and thematic areas:\n\n{text[:4000]}"
        }]
    )
    return response.content[0].text


def upsert_cv_embedding(
    airtable_id: str,
    consultant_name: str,
    role_title: str,
    cv_text: str,
    metadata: dict
) -> str:
    """Embed a consultant CV and store in Supabase."""
    try:
        embedding = get_embedding(cv_text)

        result = supabase.table("cv_embeddings").upsert({
            "airtable_consultant_id": airtable_id,
            "consultant_name": consultant_name,
            "role_title": role_title,
            "content_chunk": cv_text[:2000],
            "embedding": embedding,
            "metadata": metadata,
        }).execute()

        logger.success(f"Embedded CV: {consultant_name}")
        return result.data[0]["id"]

    except Exception as e:
        logger.error(f"Failed to embed CV for {consultant_name}: {e}")
        raise


def search_consultants(
    query_text: str,
    match_threshold: float = 0.65,
    match_count: int = 5
) -> list[dict]:
    """Semantic search for consultants matching a role requirement."""
    try:
        query_embedding = get_embedding(query_text)

        result = supabase.rpc("match_consultants", {
            "query_embedding": query_embedding,
            "match_threshold": match_threshold,
            "match_count": match_count,
        }).execute()

        return result.data

    except Exception as e:
        logger.error(f"Consultant search failed: {e}")
        return []


def check_opportunity_exists(source_url: str) -> bool:
    """Check if we've already seen this opportunity (dedup)."""
    result = supabase.table("opportunities_cache").select("id").eq(
        "source_url", source_url
    ).execute()
    return len(result.data) > 0


def find_similar_opportunity(
    title: str,
    match_threshold: float = 0.90
) -> Optional[dict]:
    """
    Semantic near-duplicate check, on top of check_opportunity_exists().
    Catches the same tender posted on multiple portals — including the
    Assortis/ICA newsletter listing something already seen via RSS or a
    scraper under a different URL — which exact URL matching can't see.

    Requires the match_opportunities() Postgres function and the
    embedding column on opportunities_cache. Fails open (returns None,
    not a duplicate) on any error, so an infra hiccup never blocks a
    possibly-real opportunity.
    """
    try:
        query_embedding = get_embedding(title)
        result = supabase.rpc("match_opportunities", {
            "query_embedding": query_embedding,
            "match_threshold": match_threshold,
            "match_count": 1,
        }).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.warning(f"Semantic duplicate check failed (non-fatal): {e}")
        return None


def store_opportunity(source_url: str, title: str, raw_text: str) -> str:
    """Store opportunity text in cache. Upsert on source_url so re-submits don't 23505."""
    result = supabase.table("opportunities_cache").upsert(
        {
            "source_url": source_url,
            "title": title,
            "raw_text": raw_text,
        },
        on_conflict="source_url",
    ).execute()
    return result.data[0]["id"]


def store_document(
    opportunity_id: str,
    file_name: str,
    file_content: bytes,
    file_type: str = "pdf"
) -> str:
    """Upload document to Supabase storage."""
    storage_path = f"opportunities/{opportunity_id}/{file_name}"

    supabase.storage.from_("cortech-documents").upload(
        storage_path,
        file_content,
        file_options={"content-type": f"application/{file_type}"}
    )

    public_url = supabase.storage.from_("cortech-documents").get_public_url(
        storage_path
    )

    supabase.table("documents").insert({
        "opportunity_id": opportunity_id,
        "file_name": file_name,
        "file_type": file_type,
        "storage_path": storage_path,
    }).execute()

    return public_url