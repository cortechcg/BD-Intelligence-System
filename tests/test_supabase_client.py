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
