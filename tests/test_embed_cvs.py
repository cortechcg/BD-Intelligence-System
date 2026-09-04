import socket
import ssl

import httpx
import pytest

import embed_cvs


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(
        f"Client error {code}",
        request=request,
        response=response,
    )


def test_ssl_handshake_timeout_is_transient():
    exc = ssl.SSLError("_ssl.c:1063: The handshake operation timed out")
    assert embed_cvs.is_transient_network_error(exc) is True


def test_dns_errno_minus_3_is_transient():
    assert embed_cvs.is_transient_network_error(
        OSError(-3, "Temporary failure in name resolution")
    )
    assert embed_cvs.is_transient_network_error(
        socket.gaierror(-3, "Temporary failure in name resolution")
    )


def test_wrapped_dns_failure_is_transient():
    cause = socket.gaierror(-3, "Temporary failure in name resolution")
    outer = httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
    outer.__cause__ = cause
    assert embed_cvs.is_transient_network_error(outer) is True


def test_httpx_timeout_connect_and_network_errors_are_transient():
    assert embed_cvs.is_transient_network_error(httpx.TimeoutException("timed out"))
    assert embed_cvs.is_transient_network_error(httpx.ConnectError("connection failed"))
    assert embed_cvs.is_transient_network_error(httpx.NetworkError("network down"))
    assert embed_cvs.is_transient_network_error(ConnectionError("connection aborted"))


def test_openai_429_is_transient():
    assert embed_cvs.is_transient_network_error(_http_status_error(429)) is True


def test_permanent_errors_are_not_transient():
    assert embed_cvs.is_transient_network_error(ValueError("OPENAI_API_KEY not set")) is False
    assert embed_cvs.is_transient_network_error(_http_status_error(401)) is False
    assert embed_cvs.is_transient_network_error(_http_status_error(400)) is False
    assert embed_cvs.is_transient_network_error(
        RuntimeError("cv_embeddings verification failed")
    ) is False
    assert embed_cvs.is_transient_network_error(
        ssl.SSLCertVerificationError("certificate verify failed")
    ) is False


def test_retry_same_operation_then_succeeds():
    sleeps = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(-3, "Temporary failure in name resolution")
        return "ok"

    assert (
        embed_cvs.retry_on_transient(
            flaky,
            label="Ahmed Adan Hassan",
            backoffs=(5.0, 15.0),
            sleep_fn=sleeps.append,
        )
        == "ok"
    )
    assert calls["n"] == 2
    assert sleeps == [5.0]


def test_retry_gives_up_after_backoffs():
    sleeps = []

    def always_dns():
        raise OSError(-3, "Temporary failure in name resolution")

    with pytest.raises(OSError):
        embed_cvs.retry_on_transient(
            always_dns,
            label="Ian Luke Munyovi",
            backoffs=(5.0, 15.0),
            sleep_fn=sleeps.append,
        )
    assert sleeps == [5.0, 15.0]


def test_permanent_error_does_not_retry():
    sleeps = []

    def boom():
        raise ValueError("bad key")

    with pytest.raises(ValueError):
        embed_cvs.retry_on_transient(
            boom, label="Ada", backoffs=(5.0, 15.0), sleep_fn=sleeps.append
        )
    assert sleeps == []


def test_circuit_pauses_after_consecutive_failures():
    sleeps = []
    circuit = embed_cvs.NetworkCircuit(
        threshold=3, pause_seconds=45.0, sleep_fn=sleeps.append
    )
    circuit.note_transient_failure()
    circuit.note_transient_failure()
    assert sleeps == []
    circuit.note_transient_failure()
    assert sleeps == [45.0]
    assert circuit.consecutive == 0


def test_already_embedded_cv_skips_openai(monkeypatch):
    record = {
        "id": "recABC",
        "fields": {
            "full_name": "Ada Lovelace",
            "cv_text": "experienced evaluator " * 10,
        },
    }
    monkeypatch.setattr(
        embed_cvs, "existing_embedding_row_id", lambda *a, **k: "emb-already"
    )

    def forbid_openai(_text):
        raise AssertionError("OpenAI must not be called when a row already exists")

    monkeypatch.setattr(embed_cvs, "get_embedding_openai", forbid_openai)
    status = embed_cvs.embed_consultant_cv(
        supabase=None,
        consultant_table=None,
        consultant_record=record,
        use_openai=True,
    )
    assert status == "skipped"


def test_failed_then_retry_success_counts_as_embedded(monkeypatch):
    monkeypatch.setattr(embed_cvs.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky_embed(_text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(-3, "Temporary failure in name resolution")
        return [0.1, 0.2]

    monkeypatch.setattr(embed_cvs, "existing_embedding_row_id", lambda *a, **k: "")
    monkeypatch.setattr(embed_cvs, "get_embedding_openai", flaky_embed)
    monkeypatch.setattr(
        embed_cvs,
        "store_single_embedding_row",
        lambda **k: ("emb-new", "inserted", 0),
    )

    class Table:
        def update(self, *args, **kwargs):
            assert kwargs.get("typecast") is True

    record = {
        "id": "recXYZ",
        "fields": {
            "full_name": "Ahmed Adan Hassan",
            "cv_text": "monitoring evaluation specialist " * 10,
        },
    }
    status = embed_cvs.embed_consultant_cv(
        supabase=object(),
        consultant_table=Table(),
        consultant_record=record,
        use_openai=True,
    )
    assert status == "embedded"
    assert calls["n"] == 2


def test_airtable_retry_strategy_does_not_include_429():
    assert 429 not in embed_cvs.AIRTABLE_RETRY_STATUS_FORCELIST
    assert embed_cvs.EMBEDDING_MODEL == "text-embedding-3-small"
