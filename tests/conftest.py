"""Offline tests must never reach a paid model.

An unstubbed generator inside a drafting test used to fall through to the
real Anthropic client and spend money (and make the assertion depend on what
the model said). Any offline test that reaches the client now fails loudly.
The hosted live tests (tests/test_live_*.py) are exempt.
"""
from __future__ import annotations

import pytest


class OfflineModelCall(RuntimeError):
    pass


def _refuse(*_args, **_kwargs):
    raise OfflineModelCall(
        "an offline test reached the Anthropic client; stub the generator or utils.llm.complete"
    )


@pytest.fixture(autouse=True)
def _no_paid_model_calls(request, monkeypatch):
    if request.node.path.name.startswith("test_live_"):
        return
    import populate_airtable
    import utils.llm

    # The two call sites. config.get_anthropic_client itself stays real so its
    # own construction test (no network) still runs.
    monkeypatch.setattr(utils.llm, "get_anthropic_client", _refuse)
    monkeypatch.setattr(populate_airtable, "get_anthropic_client", _refuse)
