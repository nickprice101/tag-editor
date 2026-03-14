import importlib
import os
import sys
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))


@contextmanager
def temp_mp3_path():
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.unlink(path)


def load_real_app_deps():
    # Other test modules stub these packages at import time. Clear them here so
    # this file always uses the real installed libraries regardless of order.
    for name in ("app", "PIL", "PIL.Image", "mutagen.id3", "mutagen.mp3"):
        sys.modules.pop(name, None)

    app = importlib.import_module("app")
    id3_mod = importlib.import_module("mutagen.id3")
    return (
        app,
        id3_mod.ID3,
        id3_mod.TPE1,
        id3_mod.TPE2,
        id3_mod.TXXX,
    )


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


class _FakeImage:
    def __init__(self, payload: bytes):
        self.payload = payload

    def convert(self, mode: str):
        assert mode == "RGB"
        return self

    def save(self, out, format: str, quality: int):
        assert format == "JPEG"
        assert quality == 92
        out.write(self.payload)


def test_upsert_id3_writes_cover_art_apic_from_art_url():
    app, ID3, _, _, _ = load_real_app_deps()
    with temp_mp3_path() as path:
        ID3().save(path)

        with patch.object(app, "http_get", return_value=_FakeResponse(b"source-red")), patch.object(
            app.Image, "open", return_value=_FakeImage(b"jpeg-red")
        ):
            app.upsert_id3(path, {"title": "x", "art_url": "https://example.test/cover.jpg"})

        tags = ID3(path)
        pics = tags.getall("APIC")
        assert len(pics) == 1
        assert pics[0].type == 3
        assert pics[0].mime == "image/jpeg"
        assert pics[0].desc == "Cover"
        assert pics[0].data == b"jpeg-red"


def test_upsert_id3_replaces_existing_apic_when_new_art_url_is_provided():
    app, ID3, _, _, _ = load_real_app_deps()
    with temp_mp3_path() as path:
        ID3().save(path)

        with patch.object(app, "http_get", return_value=_FakeResponse(b"source-blue")), patch.object(
            app.Image, "open", return_value=_FakeImage(b"jpeg-blue")
        ):
            app.upsert_id3(path, {"art_url": "https://example.test/old.jpg"})

        first = ID3(path).getall("APIC")
        assert len(first) == 1

        with patch.object(app, "http_get", return_value=_FakeResponse(b"source-green")), patch.object(
            app.Image, "open", return_value=_FakeImage(b"jpeg-green")
        ):
            app.upsert_id3(path, {"art_url": "https://example.test/new.jpg"})

        second = ID3(path).getall("APIC")
        assert len(second) == 1
        assert second[0].data != first[0].data


def test_read_tags_prefers_specific_albumartist_over_generic_various_artists():
    app, ID3, TPE1, TPE2, TXXX = load_real_app_deps()
    with temp_mp3_path() as path:
        tags = ID3()
        tags.setall("TPE1", [TPE1(encoding=3, text=["A Tribe Called Quest"])])
        tags.setall("TPE2", [TPE2(encoding=3, text=["Various Artists"])])
        tags.setall("TXXX", [TXXX(encoding=3, desc="ALBUMARTIST", text=["A Tribe Called Quest"])])
        tags.save(path)

        fake_mp3 = type(
            "FakeMp3",
            (),
            {"info": type("FakeInfo", (), {"length": 0.0, "bitrate": 0, "sample_rate": 0})()},
        )()
        with patch.object(app, "MP3", return_value=fake_mp3):
            data = app.read_tags_and_audio(path)

        assert data["albumartist"] == "A Tribe Called Quest"


def test_upsert_id3_rewrites_tpe2_from_authoritative_albumartist():
    app, ID3, _, TPE2, TXXX = load_real_app_deps()
    with temp_mp3_path() as path:
        tags = ID3()
        tags.setall("TPE2", [TPE2(encoding=3, text=["Various Artists"])])
        tags.setall("TXXX", [TXXX(encoding=3, desc="ALBUMARTIST", text=["A Tribe Called Quest"])])
        tags.save(path)

        app.upsert_id3(path, {"title": "Stressed Out", "albumartist": ""})

        saved = ID3(path)
        assert app.get_text(saved, "TPE2") == "A Tribe Called Quest"
