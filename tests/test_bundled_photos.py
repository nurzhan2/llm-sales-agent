"""Zone photos are bundled into the image and re-uploaded before every send.

Incident 2026-09-30: image ids uploaded to Avito once had expired (404) and
clients saw grey placeholders instead of photos.

The real photos belong to the client and are not part of the public
repository: the integrity check runs only where they are present.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.media import bundled
from app.media.bundled import bundled_photo_path

MANIFEST = Path("app/kb/.photos_manifest.json")


def _manifest_ids() -> list[str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [e["image_id"] for entries in manifest.values() for e in entries.values()]


@pytest.mark.skipif(
    not any(bundled.BUNDLED_DIR.glob("*.jpg")),
    reason="client photos are not shipped in the public repository",
)
def test_every_catalog_photo_is_bundled():
    ids = _manifest_ids()
    assert ids
    missing = [i for i in ids if bundled_photo_path(i) is None]
    assert not missing, missing


def test_unsafe_ids_are_rejected():
    assert bundled_photo_path("../../etc/passwd") is None
    assert bundled_photo_path("") is None
    assert bundled_photo_path("nope.123") is None


def test_pipeline_uploads_fresh_copy_instead_of_reusing_id(tmp_path, monkeypatch):
    from app.pipeline import MessagePipeline as Pipeline

    known = "1234567890.synthetic"
    (tmp_path / f"{known}.jpg").write_bytes(b"\xff\xd8" + b"\0" * 20_000)
    monkeypatch.setattr(bundled, "BUNDLED_DIR", tmp_path)
    calls = []

    class Client:
        async def upload_and_send_image(self, chat_id, data, filename="photo.jpg"):
            calls.append(("upload", filename, len(data)))

        async def send_image(self, chat_id, image_id):
            calls.append(("reuse", image_id))

    fake = Pipeline.__new__(Pipeline)
    fake.avito_client = Client()
    sent = asyncio.run(Pipeline._send_photos(fake, "chat", [known, "unknown.id"]))
    assert sent == [known, "unknown.id"]
    assert calls[0] == ("upload", f"{known}.jpg", 20_002)
    assert calls[1] == ("reuse", "unknown.id")
