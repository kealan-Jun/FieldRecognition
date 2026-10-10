"""Reusable instrument_ownership fixtures and doubles."""

from tests.support.demo import app_client, scan, wait_for_job  # noqa: F401


def own(client, label, camera, operator):
    capture = scan(client, label, camera)
    iid = capture["matches"][0]["id"]
    client.put("/api/instruments/" + iid, json={"name": label, "scene": "湿实验实验台"})
    response = client.post(
        "/api/bindings",
        json={"scan_id": capture["scan_id"], "instrument_id": iid, "operator": operator},
    )
    assert response.status_code == 200, response.text
    return response.json()
