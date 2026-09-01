from database import supabase_client


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
