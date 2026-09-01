from database import airtable_client


class _Table:
    def __init__(self):
        self.payload = None

    def create(self, payload, typecast):
        self.payload = payload
        assert typecast is True
        return {"id": "rec-1"}


def test_create_opportunity_preserves_caller_lifecycle_state(monkeypatch):
    table = _Table()
    monkeypatch.setattr(airtable_client, "get_table", lambda name: table)
    monkeypatch.setattr(airtable_client, "_circuit_open", lambda: False)

    source = {"title": "Tender", "status": "Reviewing"}
    assert airtable_client.create_opportunity(source) == "rec-1"
    assert table.payload["status"] == "Reviewing"
    assert source == {"title": "Tender", "status": "Reviewing"}
