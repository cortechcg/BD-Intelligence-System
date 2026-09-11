from database import supabase_client


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
