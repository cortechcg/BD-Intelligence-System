import config


def test_anthropic_client_uses_the_env_key(monkeypatch):
    captured = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.api_key = kwargs.get("api_key")

    monkeypatch.setattr(config, "get_anthropic_api_key", lambda: "sk-ant-test")
    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)
    config._CLIENT_CACHE.clear()

    client = config.get_anthropic_client(timeout=1, max_retries=0)

    assert client.api_key == "sk-ant-test"
    assert captured["api_key"] == "sk-ant-test"
    assert "base_url" not in captured
