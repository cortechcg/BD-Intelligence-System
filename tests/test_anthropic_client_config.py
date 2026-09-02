import logging

import config


def test_explicit_project_key_does_not_emit_profile_shadow_warning(monkeypatch, caplog):
    """A local SDK profile must not make this app's configured key look broken."""
    monkeypatch.setattr(config, "get_anthropic_api_key", lambda: "sk-ant-test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.setenv("ANTHROPIC_PROFILE", "unrelated-local-profile")
    config._CLIENT_CACHE.clear()

    with caplog.at_level(logging.WARNING, logger="anthropic.lib.credentials._auth"):
        client = config.get_anthropic_client(timeout=1, max_retries=0)

    assert client.api_key == "sk-ant-test-key"
    assert "profile / federation auto-discovery" not in caplog.text
