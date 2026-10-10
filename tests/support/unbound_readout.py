"""Reusable unbound_readout fixtures and doubles."""

import base64
import io

from PIL import Image

from tests.support.demo import app_client, scan  # noqa: F401


def photo(app, captured_at=None, raw=None, ident="saved-photo"):
    if raw is None:
        image = io.BytesIO()
        Image.new("RGB", (400, 300), "gray").save(image, "PNG")
        raw = image.getvalue()
    return {
        "capture_id": ident,
        "camera_id": "TestCamera",
        "captured_at": captured_at or app.now(),
        "source_ref": "nas://original-photo",
        "image_base64": base64.b64encode(raw).decode(),
    }


def bind(app, client):
    capture = scan(client)
    aid = capture["matches"][0]["id"]
    client.put("/api/instruments/" + aid, json={"name": "仪器 A", "scene": "湿实验实验台"})
    return client.post(
        "/api/bindings",
        json={"scan_id": capture["scan_id"], "instrument_id": aid, "operator": "甲"},
    ).json()
