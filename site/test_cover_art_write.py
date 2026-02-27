import tempfile
from io import BytesIO
from unittest.mock import patch

from mutagen.id3 import ID3
from PIL import Image

import app


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


def _jpeg_bytes(color: str) -> bytes:
    img = Image.new("RGB", (8, 8), color=color)
    out = BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def test_upsert_id3_writes_cover_art_apic_from_art_url():
    with tempfile.NamedTemporaryFile(suffix=".mp3") as tmp:
        ID3().save(tmp.name)

        with patch.object(app, "http_get", return_value=_FakeResponse(_jpeg_bytes("red"))):
            app.upsert_id3(tmp.name, {"title": "x", "art_url": "https://example.test/cover.jpg"})

        tags = ID3(tmp.name)
        pics = tags.getall("APIC")
        assert len(pics) == 1
        assert pics[0].type == 3
        assert pics[0].mime == "image/jpeg"
        assert pics[0].desc == "Cover Art"
        assert len(pics[0].data) > 0


def test_upsert_id3_replaces_existing_apic_when_new_art_url_is_provided():
    with tempfile.NamedTemporaryFile(suffix=".mp3") as tmp:
        ID3().save(tmp.name)

        with patch.object(app, "http_get", return_value=_FakeResponse(_jpeg_bytes("blue"))):
            app.upsert_id3(tmp.name, {"art_url": "https://example.test/old.jpg"})

        first = ID3(tmp.name).getall("APIC")
        assert len(first) == 1

        with patch.object(app, "http_get", return_value=_FakeResponse(_jpeg_bytes("green"))):
            app.upsert_id3(tmp.name, {"art_url": "https://example.test/new.jpg"})

        second = ID3(tmp.name).getall("APIC")
        assert len(second) == 1
        assert second[0].data != first[0].data
