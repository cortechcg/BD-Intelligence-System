from database import airtable_client


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, data):
        self.payload = None
        self.data = data

    def update(self, payload):
        self.payload = payload
        return self

    def eq(self, *_args):
        return self

    def execute(self):
        return _Result(self.data)


def test_create_opportunity_preserves_caller_lifecycle_state(monkeypatch):
    query = _Query([{"source_url": "https://example.test/tor"}])

    class _SB:
        def table(self, name):
            assert name == "opportunity_processing"
            return query

    monkeypatch.setattr(airtable_client, "_sb", lambda: _SB())

    source = {
        "title": "Tender",
        "status": "Reviewing",
        "source_url": "https://example.test/tor",
    }
    assert airtable_client.create_opportunity(source) == "https://example.test/tor"
    assert query.payload["crm_status"] == "Reviewing"
    assert source["status"] == "Reviewing"
    assert source == {
        "title": "Tender",
        "status": "Reviewing",
        "source_url": "https://example.test/tor",
    }


def test_log_agent_action_never_raises(monkeypatch):
    class _SB:
        def table(self, _name):
            raise RuntimeError("insert failed")

    monkeypatch.setattr(airtable_client, "_sb", lambda: _SB())
    airtable_client.log_agent_action("draft", "https://example.test/tor", "wrote")
