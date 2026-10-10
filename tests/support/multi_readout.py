"""Reusable multi_readout fixtures and doubles."""

from tests.support.demo import app_client, scan, wait_for_job  # noqa: F401


def two_bindings(app, client):
    bindings = []
    for name in ("InstrumentA", "InstrumentB"):
        capture = scan(client, name)
        aid = capture["matches"][0]["id"]
        client.put("/api/instruments/" + aid, json={"name": name, "scene": "湿实验实验台"})
        b = client.post(
            "/api/bindings",
            json={"scan_id": capture["scan_id"], "instrument_id": aid, "operator": "甲"},
        )
        assert b.status_code == 200
        bindings.append(b.json())
    return bindings
