"""Tests for /api/parse_url Bandcamp headless fallback behavior."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))

for mod in (
    "acoustid",
    "mutagen",
    "mutagen.id3",
    "mutagen.mp3",
    "PIL",
    "PIL.Image",
    "flask",
    "requests",
):
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)

flask_mod = sys.modules["flask"]


class _FakeApp:
    def route(self, *a, **kw):
        return lambda f: f


flask_mod.Flask = lambda *a, **kw: _FakeApp()
flask_mod.request = None
flask_mod.Response = type("Response", (), {})
flask_mod.jsonify = lambda payload: payload

requests_mod = sys.modules["requests"]


class _ReqErr(Exception):
    def __init__(self, msg="", response=None):
        super().__init__(msg)
        self.response = response


requests_mod.RequestException = _ReqErr
requests_mod.Response = type("Response", (), {})
requests_mod.Session = type("Session", (), {"get": lambda *a, **kw: None})
requests_mod.get = lambda *a, **kw: None

id3_mod = types.ModuleType("mutagen.id3")
for sym in (
    "ID3",
    "ID3NoHeaderError",
    "TIT2",
    "TPE1",
    "TALB",
    "TPE2",
    "TCON",
    "TRCK",
    "TPUB",
    "COMM",
    "TDRC",
    "TYER",
    "TXXX",
    "TSOP",
    "TSO2",
    "APIC",
    "TBPM",
    "TMED",
    "TPOS",
    "TCMP",
    "TDOR",
    "TORY",
    "UFID",
    "TIPL",
    "TSSE",
):
    setattr(id3_mod, sym, None)
sys.modules["mutagen.id3"] = id3_mod

mp3_mod = types.ModuleType("mutagen.mp3")
mp3_mod.MP3 = None
sys.modules["mutagen.mp3"] = mp3_mod

pil_mod = types.ModuleType("PIL")
img_mod = types.ModuleType("PIL.Image")
img_mod.Image = None
sys.modules["PIL"] = pil_mod
sys.modules["PIL.Image"] = img_mod

import app as app_mod  # noqa: E402


def _fake_request(url: str):
    return types.SimpleNamespace(args={"url": url}, headers={})


def test_parse_url_uses_headless_when_bandcamp_returns_403(monkeypatch):
    monkeypatch.setattr(app_mod, "request", _fake_request("https://artist.bandcamp.com/track/demo"))
    monkeypatch.setattr(app_mod, "basic_auth_ok", lambda: True)
    monkeypatch.setattr(app_mod, "HEADLESS_ENABLED", True)

    class _Response:
        status_code = 403

    def _fail_get(*args, **kwargs):
        raise app_mod.requests.RequestException("forbidden", response=_Response())

    monkeypatch.setattr(app_mod, "bandcamp_get", _fail_get)
    monkeypatch.setattr(
        app_mod,
        "_headless_get_html",
        lambda *a, **kw: (
            '<meta property="og:title" content="Demo Track, by Demo Artist">'
            '<meta property="og:image" content="https://img.example/cover.jpg">'
        ),
    )

    payload = app_mod.api_parse_url()

    assert isinstance(payload, dict)
    assert payload["fields"]["title"] == "Demo Track"
    assert payload["fields"]["artist"] == "Demo Artist"
    assert payload["fields"]["art_url"] == "https://img.example/cover.jpg"


def test_parse_url_reports_when_bandcamp_headless_fallback_fails(monkeypatch):
    monkeypatch.setattr(app_mod, "request", _fake_request("https://artist.bandcamp.com/track/demo"))
    monkeypatch.setattr(app_mod, "basic_auth_ok", lambda: True)
    monkeypatch.setattr(app_mod, "HEADLESS_ENABLED", True)

    class _Response:
        status_code = 403

    def _fail_get(*args, **kwargs):
        raise app_mod.requests.RequestException("forbidden", response=_Response())

    monkeypatch.setattr(app_mod, "bandcamp_get", _fail_get)

    def _fail_headless(*args, **kwargs):
        raise RuntimeError("playwright unavailable")

    monkeypatch.setattr(app_mod, "_headless_get_html", _fail_headless)

    payload, status = app_mod.api_parse_url()

    assert status == 403
    assert "headless fallback failed" in payload["error"].lower()
    assert "playwright unavailable" in payload["headless_details"]


def test_parse_url_extracts_bandcamp_musicrecording_jsonld_fields(monkeypatch):
    monkeypatch.setattr(app_mod, "request", _fake_request("https://adioasis.bandcamp.com/track/dumpalltheguns-jitwam-remix"))
    monkeypatch.setattr(app_mod, "basic_auth_ok", lambda: True)

    html = """
    <html><head>
      <script type="application/ld+json">
      {
        "@type": "MusicRecording",
        "additionalProperty": [{"@type": "PropertyValue", "name": "tracknum", "value": 3}],
        "name": "Dumpalltheguns (Jitwam Remix)",
        "datePublished": "08 Mar 2024 00:00:00 GMT",
        "byArtist": {"@type": "MusicGroup", "name": "Adi Oasis"},
        "keywords": ["R&B/Soul", "disco"],
        "inAlbum": {
          "@type": "MusicAlbum",
          "name": "Dumpallthguns",
          "albumRelease": [{
            "@type": ["MusicRelease", "Product"],
            "image": ["https://f4.bcbits.com/img/a3661586943_10.jpg"],
            "recordLabel": {"@type": "MusicGroup", "name": "Unity Records"}
          }]
        },
        "image": "https://f4.bcbits.com/img/a3661586943_10.jpg"
      }
      </script>
    </head><body></body></html>
    """

    class _Ok:
        text = html

        def raise_for_status(self):
            return None

    monkeypatch.setattr(app_mod, "bandcamp_get", lambda *a, **kw: _Ok())

    payload = app_mod.api_parse_url()

    assert payload["fields"]["album"] == "Dumpallthguns"
    assert payload["fields"]["title"] == "Dumpalltheguns (Jitwam Remix)"
    assert payload["fields"]["art_url"] == "https://f4.bcbits.com/img/a3661586943_10.jpg"
    assert payload["fields"]["date"] == "2024-03-08"
    assert payload["fields"]["year"] == "2024"
    assert payload["fields"]["publisher"] == "Unity Records"
    assert payload["fields"]["artist"] == "Adi Oasis"
    assert payload["fields"]["genre"] == "R&B"
    assert payload["fields"]["track"] == "3"
