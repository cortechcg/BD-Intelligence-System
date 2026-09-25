import config


def test_qwen_client_uses_modelscope_base_url(monkeypatch):
    captured = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.api_key = kwargs.get("api_key")
            self.base_url = kwargs.get("base_url")

    monkeypatch.setattr(config, "get_qwen_api_key", lambda: "ms-test-key")
    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    config._CLIENT_CACHE.clear()

    client = config.get_qwen_client(timeout=1, max_retries=0)

    assert client.api_key == "ms-test-key"
    assert captured["base_url"] == "https://api-inference.modelscope.ai/v1"
    assert "ms-test-key" not in repr(captured.get("base_url"))
