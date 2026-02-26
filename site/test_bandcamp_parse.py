"""
Unit tests for Bandcamp search result scraping in _parse_web_search_results.
"""

import sys
import os
import re
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))

# Minimal stubs so app.py can be imported without real dependencies

# Stub out heavy imports before importing app
for mod in ("acoustid", "mutagen", "mutagen.id3", "mutagen.mp3", "PIL", "PIL.Image",
            "flask", "requests"):
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)

# Stub flask
flask_mod = sys.modules["flask"]

class _FakeApp:
    def route(self, *a, **kw):
        return lambda f: f
    def run(self, *a, **kw):
        pass

flask_mod.Flask = lambda *a, **kw: _FakeApp()
flask_mod.request = None
flask_mod.Response = type("Response", (), {})
flask_mod.jsonify = None

# Stub requests
requests_mod = sys.modules["requests"]
requests_mod.Response = type("Response", (), {})
requests_mod.Session = type("Session", (), {"get": lambda *a, **kw: None})
requests_mod.get = lambda *a, **kw: None

# Stub mutagen.id3 symbols
id3_mod = types.ModuleType("mutagen.id3")
for sym in ("ID3", "ID3NoHeaderError", "TIT2", "TPE1", "TALB", "TPE2", "TCON",
            "TRCK", "TPUB", "COMM", "TDRC", "TYER", "TXXX", "TSOP", "TSO2",
            "APIC", "TBPM", "TMED", "TPOS", "TCMP", "TDOR", "TORY", "UFID", "TIPL", "TSSE"):
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

from app import _parse_web_search_results, bandcamp_get, BANDCAMP_UA  # noqa: E402


# ---------------------------------------------------------------------------
# Representative HTML based on the Bandcamp search page structure described
# in the issue.
# ---------------------------------------------------------------------------

SAMPLE_HTML = """
<ul class="result-items">
  <li class="searchresult track">
    <div class="art">
      <img data-src="https://f4.bcbits.com/img/a0123456789_7.jpg" src="data:image/gif;base64,R0lGODlhAQABAIAAAP" />
    </div>
    <div class="result-info">
      <div class="heading">
        <a href="https://40thieves.bandcamp.com/track/dont-turn-it-off-2?from=search&amp;search_item_id=12345">Don&#39;t Turn It Off</a>
      </div>
      <div class="subhead">
        by <a href="https://40thieves.bandcamp.com">40 Thieves</a>
      </div>
      <div class="released">released January 2, 2026</div>
      <div class="itemurl">
        <a href="https://40thieves.bandcamp.com/track/dont-turn-it-off-2">https://40thieves.bandcamp.com/track/dont-turn-it-off-2</a>
      </div>
    </div>
  </li>
  <li class="searchresult track">
    <div class="art">
      <img src="https://f4.bcbits.com/img/b9999999999_7.jpg" />
    </div>
    <div class="result-info">
      <div class="heading">
        <a href="https://someartist.bandcamp.com/track/another-track">Another Track</a>
      </div>
      <div class="subhead">by Some Artist</div>
      <div class="released">released March 15, 2024</div>
    </div>
  </li>
  <li class="searchresult track">
    <div class="art">
      <img srcset="https://f4.bcbits.com/img/c111_7.jpg 1x, https://f4.bcbits.com/img/c111_14.jpg 2x" />
    </div>
    <div class="result-info">
      <div class="heading">
        <a href="https://artist2.bandcamp.com/track/lazy-img?from=search">Lazy Image Track</a>
      </div>
      <div class="subhead">by Lazy Artist</div>
      <div class="itemurl">
        <a href="https://artist2.bandcamp.com/track/lazy-img">https://artist2.bandcamp.com/track/lazy-img</a>
      </div>
    </div>
  </li>
</ul>
"""


@pytest.fixture
def results():
    return _parse_web_search_results(
        "Bandcamp",
        "https://bandcamp.com/search?q=test&item_type=t",
        SAMPLE_HTML,
        artist_q="40 Thieves",
        title_q="Don't Turn It Off",
    )


def test_correct_number_of_results(results):
    assert len(results) == 3


def test_title_extracted(results):
    assert results[0]["title"] == "Don't Turn It Off"


def test_artist_from_subhead_inline_element(results):
    """Artist must come from the 'by ...' subhead, not from title text."""
    assert results[0]["artist"] == "40 Thieves"


def test_artist_from_plain_subhead(results):
    """Artist works when subhead has no nested elements."""
    assert results[1]["artist"] == "Some Artist"


def test_canonical_url_preferred_over_heading_link(results):
    """Prefer .itemurl a[href] over .heading a[href] when available."""
    assert results[0]["url"] == "https://40thieves.bandcamp.com/track/dont-turn-it-off-2"


def test_fallback_to_heading_url_when_no_itemurl(results):
    """Fall back to .heading a[href] when .itemurl is absent."""
    assert results[1]["url"] == "https://someartist.bandcamp.com/track/another-track"


def test_thumbnail_from_data_src(results):
    """Lazy-load data-src attribute is used when src is a placeholder."""
    assert results[0]["thumb"] == "https://f4.bcbits.com/img/a0123456789_16.jpg"


def test_thumbnail_from_src(results):
    """Real src attribute is used directly."""
    assert results[1]["thumb"] == "https://f4.bcbits.com/img/b9999999999_16.jpg"


def test_thumbnail_from_srcset(results):
    """First URL from srcset is used when src/data-src are absent."""
    assert results[2]["thumb"] == "https://f4.bcbits.com/img/c111_16.jpg"


def test_released_date_parsed_to_iso(results):
    """Release date is parsed to ISO YYYY-MM-DD format."""
    assert results[0].get("released") == "2026-01-02"


def test_released_date_second_result(results):
    assert results[1].get("released") == "2024-03-15"


def test_no_released_when_absent(results):
    """No 'released' key when .released element is missing."""
    assert "released" not in results[2]


def test_source_is_bandcamp(results):
    for r in results:
        assert r["source"] == "Bandcamp"


def test_score_is_float(results):
    for r in results:
        assert isinstance(r["score"], float)
        assert 0.0 <= r["score"] <= 100.0


def test_direct_url_flag(results):
    for r in results:
        assert r["direct_url"] is True
        assert r["is_fallback"] is False


def test_no_keyerror_on_missing_attrs():
    """Defensive: no crash when expected elements/attrs are absent."""
    html = """
    <ul>
      <li class="searchresult track">
        <div class="heading"><a href="https://x.bandcamp.com/track/t">Title Only</a></div>
      </li>
    </ul>
    """
    results = _parse_web_search_results(
        "Bandcamp", "https://bandcamp.com/search?q=test", html,
        artist_q="", title_q="Title Only",
    )
    assert len(results) == 1
    assert results[0]["title"] == "Title Only"
    assert results[0]["artist"] == ""
    assert "thumb" not in results[0]
    assert "released" not in results[0]


# ---------------------------------------------------------------------------
# Debug diagnostics (_debug_info)
# ---------------------------------------------------------------------------

def test_debug_info_populated_on_match():
    """_debug_info receives a count message when results are found."""
    debug: list = []
    _parse_web_search_results(
        "Bandcamp", "https://bandcamp.com/search?q=test", SAMPLE_HTML,
        artist_q="40 Thieves", title_q="Don't Turn It Off",
        _debug_info=debug,
    )
    assert any("li.searchresult count=3" in msg for msg in debug)


def test_debug_info_alt_selectors_when_no_match():
    """When no li.searchresult is found, alt-selector counts are reported."""
    empty_html = "<html><body><p>No results</p></body></html>"
    debug: list = []
    results = _parse_web_search_results(
        "Bandcamp", "https://bandcamp.com/search?q=test", empty_html,
        artist_q="artist", title_q="title",
        _debug_info=debug,
    )
    # The function returns a fallback entry when nothing is found
    assert len(results) == 1
    assert results[0]["is_fallback"] is True
    assert any("li.searchresult count=0" in msg for msg in debug)
    # At least one message should mention the alt-selector probe
    assert any("alt selectors" in msg for msg in debug)


def test_debug_info_not_required():
    """Omitting _debug_info (default None) must not raise."""
    results = _parse_web_search_results(
        "Bandcamp", "https://bandcamp.com/search?q=test", SAMPLE_HTML,
        artist_q="40 Thieves", title_q="Don't Turn It Off",
    )
    assert len(results) == 3


def test_debug_info_none_by_default():
    """Passing _debug_info=None explicitly must not raise."""
    debug = None
    results = _parse_web_search_results(
        "Bandcamp", "https://bandcamp.com/search?q=test", SAMPLE_HTML,
        artist_q="", title_q="",
        _debug_info=debug,
    )
    assert len(results) == 3


# ---------------------------------------------------------------------------
# bandcamp_get helper
# ---------------------------------------------------------------------------

def test_bandcamp_get_uses_chrome_ua(monkeypatch):
    """bandcamp_get must pass the BANDCAMP_UA as User-Agent."""
    captured = {}

    def fake_get(url, headers=None, **kwargs):
        captured["headers"] = headers or {}
        return object()

    import sys
    sys.modules["requests"].get = fake_get

    bandcamp_get("https://bandcamp.com/search?q=test", timeout=10)

    assert captured["headers"].get("User-Agent") == BANDCAMP_UA


def test_bandcamp_get_sends_browser_headers(monkeypatch):
    """bandcamp_get must include Accept, Accept-Language, Referer, Cache-Control."""
    captured = {}

    def fake_get(url, headers=None, **kwargs):
        captured["headers"] = headers or {}
        return object()

    import sys
    sys.modules["requests"].get = fake_get

    bandcamp_get("https://bandcamp.com/search?q=test", timeout=10)

    assert "Accept" in captured["headers"]
    assert "Accept-Language" in captured["headers"]
    assert captured["headers"].get("Referer") == "https://bandcamp.com/"
    assert captured["headers"].get("Cache-Control") == "no-cache"


def test_bandcamp_ua_is_chrome():
    """BANDCAMP_UA must contain a mainstream Chrome identifier."""
    assert "Chrome" in BANDCAMP_UA
    assert "Mozilla/5.0" in BANDCAMP_UA
