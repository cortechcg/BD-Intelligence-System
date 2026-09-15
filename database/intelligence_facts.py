"""Fail-open persistence for cited award and relationship facts (Phase 5).

Missing tables behave like organizations / content_hash: log once, return
empty, never crash. Do not apply supabase_migration_award_relationships.sql
from application code.
"""

from __future__ import annotations

from loguru import logger

from intelligence.competitors import AwardFact
from intelligence.organizations import STATUS_UNKNOWN
from intelligence.relationships import RelationshipEdge

_MISSING = (
    "award_observations / relationship_edges missing — apply "
    "supabase_migration_award_relationships.sql. Phase 5 facts fail-open."
)

_facts_available: bool | None = None


def reset_intelligence_facts_status() -> None:
    """Test helper."""
    global _facts_available
    _facts_available = None


def _is_missing_relation(exc: Exception) -> bool:
    msg = str(exc).lower()
    tokens = ("award_observations", "relationship_edges", "organizations")
    if not any(t in msg for t in tokens):
        if "pgrst205" in msg or "pgrst204" in msg or "42p01" in msg or "42703" in msg:
            return True
        return False
    return (
        "pgrst205" in msg
        or "pgrst204" in msg
        or "42p01" in msg
        or "42703" in msg
        or "schema cache" in msg
        or "does not exist" in msg
        or "could not find" in msg
        or "unknown column" in msg
    )


def _is_unique_violation(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "23505" in msg or "duplicate" in msg or "unique" in msg


def _mark_missing(exc: Exception) -> None:
    global _facts_available
    _facts_available = False
    logger.warning(f"{_MISSING} Cause: {exc}")


def _client():
    from database.supabase_client import get_supabase

    return get_supabase()


def facts_tables_available() -> bool:
    global _facts_available
    if _facts_available is False:
        return False
    if _facts_available is True:
        return True
    try:
        _client().table("award_observations").select("id").limit(1).execute()
        _client().table("relationship_edges").select("id").limit(1).execute()
        _facts_available = True
        return True
    except Exception as e:
        if _is_missing_relation(e):
            _mark_missing(e)
            return False
        logger.warning(f"Phase 5 facts probe failed (not treating as missing): {e}")
        return False


def _org_id(fact_match) -> str | None:
    if fact_match is None:
        return None
    org_id = getattr(fact_match, "organization_id", None)
    if not org_id:
        return None
    return str(org_id)


def persist_award_fact(fact: AwardFact) -> None:
    if not isinstance(fact, AwardFact):
        return
    if fact.evidence_status != "VERIFIED":
        return
    if not fact.source_url or not fact.observed_name or not fact.excerpt:
        return
    if not _loose_in(fact.observed_name, fact.excerpt):
        return
    if _facts_available is False:
        return
    match = fact.match
    org_id = None
    if match is not None:
        try:
            from database import organizations as org_store

            persisted = org_store.persist_match(match, entity_kind="unknown")
            org_id = persisted.organization_id
            match = persisted
        except Exception:
            org_id = _org_id(match)
    try:
        sb = _client()
        payload = {
            "observed_name": fact.observed_name,
            "match_method": match.method if match else "new_candidate",
            "match_confidence": float(match.confidence) if match else 0.0,
            "match_status": match.status if match else STATUS_UNKNOWN,
            "evidence_status": "VERIFIED",
            "opportunity_title": fact.opportunity_title or "",
            "source_url": fact.source_url,
            "excerpt": fact.excerpt,
            "source_kind": "award_notice",
        }
        if org_id:
            payload["organization_id"] = org_id
        sb.table("award_observations").upsert(
            payload, on_conflict="source_url,observed_name"
        ).execute()
    except Exception as e:
        if _is_missing_relation(e):
            _mark_missing(e)
            return
        if _is_unique_violation(e):
            return
        logger.warning(f"award_observations upsert failed (fail-open): {e}")


def persist_relationship_edge(edge: RelationshipEdge) -> None:
    if not isinstance(edge, RelationshipEdge):
        return
    if edge.evidence_status != "VERIFIED":
        return
    if not edge.document_name or not edge.excerpt or not edge.observed_name:
        return
    if not _loose_in(edge.observed_name, edge.excerpt):
        return
    if _facts_available is False:
        return
    match = edge.match
    org_id = None
    if match is not None:
        try:
            from database import organizations as org_store

            persisted = org_store.persist_match(match, entity_kind="unknown")
            org_id = persisted.organization_id
            match = persisted
        except Exception:
            org_id = _org_id(match)
    try:
        sb = _client()
        payload = {
            "observed_name": edge.observed_name,
            "relationship_kind": edge.relationship_kind,
            "cortech_role": edge.cortech_role or "",
            "document_name": edge.document_name,
            "chunk_id": edge.chunk_id,
            "excerpt": edge.excerpt,
            "match_method": match.method if match else "new_candidate",
            "match_confidence": float(match.confidence) if match else 0.0,
            "match_status": match.status if match else STATUS_UNKNOWN,
            "evidence_status": "VERIFIED",
        }
        if org_id:
            payload["organization_id"] = org_id
        sb.table("relationship_edges").upsert(
            payload, on_conflict="document_name,observed_name,relationship_kind"
        ).execute()
    except Exception as e:
        if _is_missing_relation(e):
            _mark_missing(e)
            return
        if _is_unique_violation(e):
            return
        logger.warning(f"relationship_edges upsert failed (fail-open): {e}")


def _loose_in(name: str, excerpt: str) -> bool:
    n = " ".join(re_alnum(name).split())
    h = " ".join(re_alnum(excerpt).split())
    return bool(n) and n in h


def re_alnum(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold())


def load_award_observations(limit: int = 50) -> list[dict]:
    if _facts_available is False:
        return []
    try:
        result = (
            _client()
            .table("award_observations")
            .select(
                "observed_name, source_url, excerpt, opportunity_title, "
                "match_status, match_method, organization_id, evidence_status"
            )
            .limit(max(1, min(int(limit), 200)))
            .execute()
        )
        rows = result.data or []
        return [r for r in rows if isinstance(r, dict) and r.get("source_url") and r.get("excerpt")]
    except Exception as e:
        if _is_missing_relation(e):
            _mark_missing(e)
            return []
        logger.warning(f"award_observations load failed (fail-open): {e}")
        return []


def load_relationship_edges(limit: int = 50) -> list[dict]:
    if _facts_available is False:
        return []
    try:
        result = (
            _client()
            .table("relationship_edges")
            .select(
                "observed_name, relationship_kind, document_name, chunk_id, "
                "excerpt, cortech_role, match_status, match_method, "
                "organization_id, evidence_status"
            )
            .limit(max(1, min(int(limit), 200)))
            .execute()
        )
        rows = result.data or []
        return [
            r
            for r in rows
            if isinstance(r, dict) and r.get("document_name") and r.get("excerpt")
        ]
    except Exception as e:
        if _is_missing_relation(e):
            _mark_missing(e)
            return []
        logger.warning(f"relationship_edges load failed (fail-open): {e}")
        return []


def load_proposal_embedding_rows(limit: int = 200) -> list[dict]:
    """Fail-open scan of proposal_embeddings for relationship extraction."""
    try:
        result = (
            _client()
            .table("proposal_embeddings")
            .select("airtable_proposal_id, project_title, content_chunk, metadata")
            .limit(max(1, min(int(limit), 500)))
            .execute()
        )
        return [r for r in (result.data or []) if isinstance(r, dict)]
    except Exception as e:
        logger.warning(f"proposal_embeddings relationship scan failed (fail-open): {e}")
        return []
