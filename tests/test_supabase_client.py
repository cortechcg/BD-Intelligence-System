from database import supabase_client
from pathlib import Path


PGRST205 = (
    "PGRST205: Could not find the table 'public.opportunity_processing' "
    "in the schema cache"
)


class _MissingLedger:
    def table(self, name):
        raise RuntimeError(PGRST205)

    def rpc(self, *args, **kwargs):
        raise RuntimeError(PGRST205)


def _reset_ledger(monkeypatch):
    supabase_client.reset_opportunity_ledger_status()
    monkeypatch.setattr(supabase_client, "supabase", _MissingLedger())
    monkeypatch.setattr(supabase_client, "_opportunity_ledger_available", None)


def test_supabase_client_is_created_lazily_and_reused(monkeypatch):
    calls = []
    fake_client = object()
    monkeypatch.setattr(supabase_client, "_supabase_client", None)
    monkeypatch.setattr(supabase_client, "SUPABASE_URL", "https://project.example")
    monkeypatch.setattr(supabase_client, "SUPABASE_SERVICE_KEY", "service-key")
    monkeypatch.setattr(
        supabase_client,
        "create_client",
        lambda url, key: calls.append((url, key)) or fake_client,
    )

    assert calls == []
    assert supabase_client.get_supabase() is fake_client
    assert supabase_client.get_supabase() is fake_client
    assert calls == [("https://project.example", "service-key")]


def test_supabase_storage_fails_loudly_when_config_is_missing(monkeypatch):
    monkeypatch.setattr(supabase_client, "_supabase_client", None)
    monkeypatch.setattr(supabase_client, "SUPABASE_URL", None)
    monkeypatch.setattr(supabase_client, "SUPABASE_SERVICE_KEY", None)

    try:
        supabase_client.get_supabase()
        raise AssertionError("missing configuration should not construct a client")
    except ValueError as exc:
        assert "SUPABASE_URL" in str(exc)


def test_pgrst205_does_not_treat_urls_as_new(monkeypatch):
    _reset_ledger(monkeypatch)
    assert supabase_client.check_opportunity_exists(
        "https://www.somalijobs.com/tenders/1/endline"
    ) is True
    assert supabase_client.opportunity_ledger_available() is False


def test_pgrst205_bulk_claim_is_refused_manual_submit_still_proceeds(monkeypatch):
    _reset_ledger(monkeypatch)
    url = "https://www.somalijobs.com/tenders/2/evaluation"
    assert supabase_client.claim_opportunity_processing(url, "Tender") is None
    token = supabase_client.claim_opportunity_processing(
        url, "Tender", force=True
    )
    assert isinstance(token, str) and token


class _RpcResult:
    def __init__(self, data):
        self.data = data


class _ClaimLedger:
    def __init__(self, behavior="ok"):
        self.behavior = behavior
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))

        class _Call:
            def execute(_self, ledger=self, rpc_name=name, rpc_params=params):
                if rpc_name != "claim_opportunity_processing":
                    raise AssertionError(rpc_name)
                token = rpc_params["p_claim_token"]
                if ledger.behavior == "ok":
                    return _RpcResult([{
                        "acquired": True,
                        "state": "processing",
                        "attempt_count": 1,
                        "claim_token": token,
                    }])
                if ledger.behavior == "busy":
                    return _RpcResult([{
                        "acquired": False,
                        "state": "processing",
                        "attempt_count": 1,
                        "claim_token": "",
                    }])
                return _RpcResult([{
                    "acquired": True,
                    "state": "processing",
                    "attempt_count": 1,
                    "claim_token": "someone-elses-lease",
                }])

        return _Call()


def test_claim_succeeds_when_ledger_echoes_token(monkeypatch):
    supabase_client.reset_opportunity_ledger_status()
    ledger = _ClaimLedger("ok")
    monkeypatch.setattr(supabase_client, "supabase", ledger)
    token = supabase_client.claim_opportunity_processing(
        "https://www.somalijobs.com/tenders/3/eval",
        "Endline",
    )
    assert isinstance(token, str) and token
    assert ledger.calls[0][0] == "claim_opportunity_processing"
    assert ledger.calls[0][1]["p_force"] is False
    assert supabase_client._opportunity_ledger_available is True


def test_claim_refuses_active_lease_and_token_mismatch(monkeypatch):
    supabase_client.reset_opportunity_ledger_status()
    busy = _ClaimLedger("busy")
    monkeypatch.setattr(supabase_client, "supabase", busy)
    assert supabase_client.claim_opportunity_processing(
        "https://www.somalijobs.com/tenders/4/eval",
        "Taken",
    ) is None

    supabase_client.reset_opportunity_ledger_status()
    mismatch = _ClaimLedger("mismatch")
    monkeypatch.setattr(supabase_client, "supabase", mismatch)
    assert supabase_client.claim_opportunity_processing(
        "https://www.somalijobs.com/tenders/5/eval",
        "Mismatch",
    ) is None


def test_get_embedding_uses_sanitized_env_key_and_does_not_dump_401_body(monkeypatch):
    monkeypatch.setattr(
        supabase_client, "get_openai_api_key", lambda: "sk-test-openai-key"
    )

    class _Response:
        status_code = 401
        text = '{"error":{"message":"Your API key has been invalidated."}}'

        def json(self):
            return {}

    monkeypatch.setattr(supabase_client.httpx, "post", lambda *a, **k: _Response())
    try:
        supabase_client.get_embedding("evaluation Somalia")
        raise AssertionError("401 must raise EmbeddingError")
    except supabase_client.EmbeddingError as exc:
        assert exc.auth is True
        assert "Your API key has been invalidated." not in str(exc)


def test_store_opportunity_writes_content_hash(monkeypatch):
    from types import SimpleNamespace
    from utils.hashing import content_hash

    backend = {"upserts": [], "error": None, "lookup": []}

    class _Table:
        def upsert(self, row, on_conflict=None):
            backend["upserts"].append((row, on_conflict))

            class _Call:
                def execute(_self):
                    if backend["error"]:
                        raise RuntimeError(backend["error"])
                    return SimpleNamespace(data=[{"id": "row-1", **row}])

            return _Call()

        def select(self, *_args):
            return self

        def eq(self, *_args):
            return self

        def limit(self, *_args):
            return self

        def execute(self):
            return SimpleNamespace(data=backend["lookup"])

    class _Client:
        def table(self, name):
            assert name == "opportunities_cache"
            return _Table()

    monkeypatch.setattr(supabase_client, "supabase", _Client())
    monkeypatch.setattr(supabase_client, "get_embedding", lambda _t: [0.1, 0.2])

    body = "Terms of Reference for an evaluation in Somalia."
    row_id = supabase_client.store_opportunity(
        "https://procurement.example/tender",
        "Endline",
        body,
    )
    assert row_id == "row-1"
    row, conflict = backend["upserts"][0]
    assert conflict == "source_url"
    assert row["content_hash"] == content_hash(body)
    assert row["source_url"] == "https://procurement.example/tender"


def test_store_opportunity_retries_without_hash_when_column_missing(monkeypatch):
    from types import SimpleNamespace

    backend = {"upserts": [], "error": (
        "PGRST204: Could not find the 'content_hash' column of "
        "'opportunities_cache' in the schema cache"
    )}

    class _Table:
        def upsert(self, row, on_conflict=None):
            backend["upserts"].append(row)

            class _Call:
                def execute(_self):
                    if "content_hash" in row and backend["error"]:
                        raise RuntimeError(backend["error"])
                    return SimpleNamespace(data=[{"id": "row-2", **row}])

            return _Call()

    class _Client:
        def table(self, name):
            return _Table()

    monkeypatch.setattr(supabase_client, "supabase", _Client())
    monkeypatch.setattr(supabase_client, "get_embedding", lambda _t: [0.1])

    row_id = supabase_client.store_opportunity(
        "https://procurement.example/tender",
        "Endline",
        "Terms of Reference body text here.",
    )
    assert row_id == "row-2"
    assert "content_hash" in backend["upserts"][0]
    assert "content_hash" not in backend["upserts"][1]


def test_store_opportunity_returns_existing_id_on_content_hash_collision(monkeypatch):
    from types import SimpleNamespace
    from utils.hashing import content_hash

    body = "Identical tender body for two portals."
    digest = content_hash(body)

    class _Table:
        def upsert(self, row, on_conflict=None):
            class _Call:
                def execute(_self):
                    raise RuntimeError(
                        'duplicate key value violates unique constraint '
                        '"opportunities_cache_content_hash_uidx" (23505) content_hash'
                    )
            return _Call()

        def select(self, *_args):
            return self

        def eq(self, *_args):
            return self

        def limit(self, *_args):
            return self

        def execute(self):
            return SimpleNamespace(data=[{
                "id": "existing-id",
                "source_url": "https://first.example/tender",
                "content_hash": digest,
            }])

    class _Client:
        def table(self, name):
            return _Table()

    monkeypatch.setattr(supabase_client, "supabase", _Client())
    monkeypatch.setattr(supabase_client, "get_embedding", lambda _t: [0.1])

    assert supabase_client.store_opportunity(
        "https://second.example/tender", "Copy", body
    ) == "existing-id"


def test_find_content_hash_fail_open_when_column_missing(monkeypatch):
    class _Table:
        def select(self, *_args):
            return self

        def eq(self, *_args):
            return self

        def limit(self, *_args):
            return self

        def execute(self):
            raise RuntimeError(
                "PGRST204: Could not find the 'content_hash' column of "
                "'opportunities_cache' in the schema cache"
            )

    class _Client:
        def table(self, name):
            return _Table()

    monkeypatch.setattr(supabase_client, "supabase", _Client())
    assert supabase_client.find_opportunity_by_content_hash("abc") is None


def test_content_hash_migration_enforces_partial_unique_index():
    sql = Path("supabase_migration_content_hash.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS content_hash TEXT" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS opportunities_cache_content_hash_uidx" in sql
    assert "WHERE content_hash IS NOT NULL" in sql
    assert "organizations" not in sql.lower()
    assert "competitor" not in sql.lower()


def test_content_hash_normalizes_whitespace_and_case():
    from utils.hashing import content_hash

    assert content_hash("Hello\n\nWorld") == content_hash("hello world")
    assert content_hash("  ") == content_hash("")
    assert content_hash("A") != content_hash("B")


def test_load_snapshot_when_stage_columns_missing_logs_and_does_not_drop(monkeypatch):
    supabase_client.reset_opportunity_ledger_status()

    class _Table:
        def select(self, *_args):
            return self

        def eq(self, *_args):
            return self

        def limit(self, *_args):
            return self

        def execute(self):
            raise RuntimeError(
                "PGRST204: Could not find the 'pipeline_stage' column of "
                "'opportunity_processing' in the schema cache"
            )

    class _Client:
        def table(self, name):
            assert name == "opportunity_processing"
            return _Table()

    monkeypatch.setattr(supabase_client, "supabase", _Client())
    snap = supabase_client.load_processing_snapshot(
        "https://procurement.example/tender"
    )
    assert snap["resume_available"] is False
    assert snap["pipeline_stage"] == "discovered"
    assert supabase_client._opportunity_stage_columns_available is False


def test_persist_stage_when_rpc_missing_does_not_mark_ledger_gone(monkeypatch):
    supabase_client.reset_opportunity_ledger_status()

    class _Client:
        def rpc(self, name, params):
            assert name == "persist_opportunity_stage"

            class _Call:
                def execute(_self):
                    raise RuntimeError(
                        "PGRST202: Could not find the function "
                        "public.persist_opportunity_stage in the schema cache"
                    )
            return _Call()

    monkeypatch.setattr(supabase_client, "supabase", _Client())
    assert supabase_client.persist_opportunity_stage(
        "https://procurement.example/tender", "tok", "extracted", {"full_text": "x"},
    ) is False
    assert supabase_client._opportunity_stage_columns_available is False
    assert supabase_client._opportunity_ledger_available is not False


def test_check_opportunity_stage_schema_fails_when_columns_missing(monkeypatch):
    supabase_client.reset_opportunity_ledger_status()

    class _Table:
        def __init__(self):
            self._col = ""

        def select(self, col):
            self._col = col
            return self

        def limit(self, *_args):
            return self

        def execute(self):
            raise RuntimeError(
                f"42703: column opportunity_processing.{self._col} does not exist"
            )

    class _Client:
        def table(self, name):
            assert name == "opportunity_processing"
            return _Table()

    monkeypatch.setattr(supabase_client, "supabase", _Client())
    missing = supabase_client.check_opportunity_stage_schema()
    assert missing == list(supabase_client.REQUIRED_STAGE_COLUMNS)


def test_fact_payload_from_stored_labels_drops_non_strings():
    assert supabase_client.fact_payload_from_stored_labels(
        ["Evaluation", {"area": "WASH"}, 12, ""],
        "Somalia",
        "  UNDP  ",
    ) == {
        "thematic_areas": ["Evaluation"],
        "locations": ["Somalia"],
        "donor": "UNDP",
    }
    assert supabase_client.fact_payload_from_stored_labels(
        [{"area": "WASH"}], None, None
    ) == {}
    assert supabase_client.fact_payload_from_stored_labels(None, None, None) == {}


def test_backfill_opportunity_facts_from_ledger_copies_null_only(monkeypatch):
    """Ledger string labels fill SQL-NULL cache facts; never invent or overwrite."""
    from types import SimpleNamespace

    cache = {
        "https://example.com/null-themes": {
            "title": "Null themes row",
            "thematic_areas": None,
            "locations": None,
            "donor": None,
        },
        "https://example.com/already-labeled": {
            "title": "Already labeled",
            "thematic_areas": ["Keep Me"],
            "locations": ["Nairobi"],
            "donor": "Existing Donor",
        },
        "https://example.com/no-labels": {
            "title": "No ledger labels",
            "thematic_areas": None,
            "locations": None,
            "donor": None,
        },
    }
    updates = []
    ledger_pages = [
        [
            {
                "source_url": "https://example.com/null-themes",
                "themes": ["MEL", "Evaluation"],
                "locs": ["Somalia"],
                "donor": "UNICEF",
            },
            {
                "source_url": "https://example.com/already-labeled",
                "themes": ["Should Not Overwrite"],
                "locs": ["Should Not Overwrite"],
                "donor": "Should Not Overwrite",
            },
            {
                "source_url": "https://example.com/no-labels",
                "themes": [{"area": "WASH"}, 3],
                "locs": None,
                "donor": None,
            },
            {
                "source_url": "https://example.com/scored-no-cache",
                "themes": ["Scored only"],
                "locs": ["Puntland"],
                "donor": "FAO",
            },
        ]
    ]

    class _CacheQuery:
        def __init__(self, table):
            self._table = table
            self._eq = None
            self._write = None

        def select(self, *_a):
            return self

        def eq(self, col, val):
            self._eq = (col, val)
            return self

        def limit(self, *_a):
            return self

        def update(self, write):
            self._write = write
            return self

        def execute(self):
            if self._write is not None:
                url = self._eq[1]
                updates.append((url, dict(self._write)))
                cache[url].update(self._write)
                return SimpleNamespace(data=[cache[url]])
            url = self._eq[1] if self._eq else None
            row = cache.get(url)
            return SimpleNamespace(data=[row] if row else [])

    class _LedgerQuery:
        def __init__(self):
            self._range = (0, 199)

        def select(self, *_a):
            return self

        def range(self, start, end):
            self._range = (start, end)
            return self

        def execute(self):
            start, _end = self._range
            page_idx = start // 200
            if page_idx >= len(ledger_pages):
                return SimpleNamespace(data=[])
            return SimpleNamespace(data=ledger_pages[page_idx])

    class _Client:
        def table(self, name):
            if name == "opportunity_processing":
                return _LedgerQuery()
            if name == "opportunities_cache":
                return _CacheQuery(name)
            raise AssertionError(name)

    monkeypatch.setattr(supabase_client, "supabase", _Client())
    monkeypatch.setattr(
        supabase_client, "canonicalize_url", lambda u: u
    )

    stats = supabase_client.backfill_opportunity_facts_from_ledger()

    assert stats["updated"] == 1
    assert stats["unchanged"] == 1
    assert stats["no_cache_row"] == 1
    assert stats["no_stored_labels"] == 1
    assert "Null themes row" in stats["updated_titles"]
    assert updates == [
        (
            "https://example.com/null-themes",
            {
                "thematic_areas": ["MEL", "Evaluation"],
                "locations": ["Somalia"],
                "donor": "UNICEF",
            },
        )
    ]
    assert cache["https://example.com/already-labeled"]["thematic_areas"] == ["Keep Me"]
    assert cache["https://example.com/no-labels"]["thematic_areas"] is None
    assert "https://example.com/scored-no-cache" not in cache
