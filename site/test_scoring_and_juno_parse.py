"""
Tests for case-insensitive scoring and Juno secondary parser track_number extraction.
"""

import sys
import os
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))

# Minimal stubs so app.py can be imported without real dependencies
for mod in ("acoustid", "mutagen", "mutagen.id3", "mutagen.mp3", "PIL", "PIL.Image",
            "flask", "requests"):
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)

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

requests_mod = sys.modules["requests"]
requests_mod.Response = type("Response", (), {})
requests_mod.Session = type("Session", (), {"get": lambda *a, **kw: None})
requests_mod.get = lambda *a, **kw: None

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

from app import _score_result, _parse_web_search_results, _deduplicate_by_url  # noqa: E402


# ---------------------------------------------------------------------------
# Case-insensitive scoring tests
# ---------------------------------------------------------------------------

def test_score_case_insensitive_title():
    """Title comparison must be case-insensitive: 'Track' vs 'track' → same score as exact match."""
    score_exact = _score_result("Artist", "Track", "Artist", "Track")
    score_upper = _score_result("Artist", "TRACK", "Artist", "track")
    assert score_exact == score_upper == 100.0


def test_score_case_insensitive_artist():
    """Artist comparison must be case-insensitive: 'Artist' vs 'artist' → same score."""
    score_exact = _score_result("Artist", "Track", "Artist", "Track")
    score_mixed = _score_result("ARTIST", "track", "artist", "TRACK")
    assert score_exact == score_mixed == 100.0


def test_score_mixed_case_no_penalty():
    """Mixed-case title and artist still yield 100 when content matches ignoring case."""
    score = _score_result("Adi Oasis", "Dumpalltheguns", "ADI OASIS", "DUMPALLTHEGUNS")
    assert score == 100.0


def test_score_case_insensitive_partial_mismatch():
    """Case-insensitive comparison: different content (not just case) still penalises correctly."""
    score_match = _score_result("Artist", "Track One", "Artist", "TRACK ONE")
    score_mismatch = _score_result("Artist", "Track One", "Artist", "TRACK TWO")
    assert score_match == 100.0
    assert score_mismatch < 100.0


# ---------------------------------------------------------------------------
# Juno secondary parser: track_number extraction
# ---------------------------------------------------------------------------

JUNO_SECONDARY_HTML_HREF = """
<html><body>
<div class="jd-listing-item">
  <div class="juno-artist"><a href="#">Some Artist</a></div>
  <div class="juno-title">
    <a class="juno-title" href="/products/some-track/1234/?track_number=3">Some Track</a>
  </div>
  <a href="https://www.junodownload.com/products/some-track/1234/?track_number=3">link</a>
</div>
</body></html>
"""

JUNO_SECONDARY_HTML_ONCLICK = """
<html><body>
<div class="jd-listing-item">
  <div class="juno-artist"><a href="#">Another Artist</a></div>
  <div class="juno-title">
    <a class="juno-title"
       href="/products/another-track/5678/"
       onclick="show_pl_context_menu(event, { artist: 'Another Artist', title: 'Another Track', track_number: '7', release_id: '5678' });">Another Track</a>
  </div>
  <a href="https://www.junodownload.com/products/another-track/5678/">link</a>
</div>
</body></html>
"""

JUNO_SECONDARY_HTML_NO_TRACK = """
<html><body>
<div class="jd-listing-item">
  <div class="juno-artist"><a href="#">No Track Artist</a></div>
  <div class="juno-title">
    <a class="juno-title" href="/products/no-track/9999/">No Track Number</a>
  </div>
  <a href="https://www.junodownload.com/products/no-track/9999/">link</a>
</div>
</body></html>
"""


def test_juno_secondary_track_number_from_href():
    """track_number is extracted from the a.juno-title href query string."""
    results = _parse_web_search_results(
        "Juno",
        "https://www.junodownload.com/search/?q=test",
        JUNO_SECONDARY_HTML_HREF,
        artist_q="Some Artist",
        title_q="Some Track",
    )
    assert len(results) == 1
    assert results[0].get("track_number") == "3"


def test_juno_secondary_track_number_from_onclick_fallback():
    """track_number is extracted from onclick when not in href query string."""
    results = _parse_web_search_results(
        "Juno",
        "https://www.junodownload.com/search/?q=test",
        JUNO_SECONDARY_HTML_ONCLICK,
        artist_q="Another Artist",
        title_q="Another Track",
    )
    assert len(results) == 1
    assert results[0].get("track_number") == "7"


def test_juno_secondary_no_track_number_when_absent():
    """track_number key is absent when neither href nor onclick contains it."""
    results = _parse_web_search_results(
        "Juno",
        "https://www.junodownload.com/search/?q=test",
        JUNO_SECONDARY_HTML_NO_TRACK,
        artist_q="No Track Artist",
        title_q="No Track Number",
    )
    assert len(results) == 1
    assert "track_number" not in results[0]


def test_juno_secondary_href_takes_precedence_over_onclick():
    """When href has track_number, it is used even if onclick also has one."""
    html = """
    <html><body>
    <div class="jd-listing-item">
      <div class="juno-artist"><a href="#">Artist</a></div>
      <div class="juno-title">
        <a class="juno-title"
           href="/products/track/111/?track_number=2"
           onclick="show_pl_context_menu(event, { track_number: '99' });">Title</a>
      </div>
      <a href="https://www.junodownload.com/products/track/111/?track_number=2">link</a>
    </div>
    </body></html>
    """
    results = _parse_web_search_results(
        "Juno",
        "https://www.junodownload.com/search/?q=test",
        html,
        artist_q="Artist",
        title_q="Title",
    )
    assert len(results) == 1
    assert results[0].get("track_number") == "2"

JUNO_SECONDARY_HTML_METADATA = """
<html><body>
<div class="jd-listing-item-track" data-ua_location="track">
  <div class="lit-img"><a href="/products/turn-up-tunes-vol-08/7421830-02/?track_number=4"><img class="img-fluid-fill img-rnd" src="https://imagescdn.junodownload.com/75/CS7421830-02A-TN.jpg"></a></div>
  <div class="lit-artist-title jq_highlight"><div class="juno-artist"><a href="/artists/Selda/">Selda</a></div><a class="juno-title" href="/products/turn-up-tunes-vol-08/7421830-02/?track_number=4">Set It Off (E.M.C.K. extended remix)</a></div>
  <div class="lit-label-genre jq_highlight d-none d-md-block"><a class="juno-label" href="/labels/Play+This%21/">Play This!</a><br>Funky/Club House</div>
  <div class="lit-date-length-tempo text-right d-none d-sm-block">5:38 / 124 BPM</div>
</div>
</body></html>
"""


def test_juno_secondary_extracts_label_genre_bpm_and_track_number():
    results = _parse_web_search_results(
        "Juno",
        "https://www.junodownload.com/search/?q=test",
        JUNO_SECONDARY_HTML_METADATA,
        artist_q="Selda",
        title_q="Set It Off",
    )
    assert len(results) == 1
    assert results[0].get("track_number") == "4"
    assert results[0].get("label") == "Play This!"
    assert results[0].get("genre") == "Funky/Club House"
    assert results[0].get("bpm") == "124"


JUNO_SECONDARY_HTML_ATC_TRACK = """
<html><body>
<div class="jd-listing-item-track" data-ua_location="track">
  <div class="lit-artist-title"><div class="juno-artist"><a href="/artists/Test/">Test Artist</a></div><a class="juno-title" href="/products/test-release/7421830-02/">Test Track</a></div>
  <div class="lit-date-length-tempo">5:38 / 124 BPM</div>
  <div class="lit-actions"><button type="button" class="btn btn-widget-atc" onclick="uaAddCartEvent(event); return addToCart(7421830, 2, 4);">🛒1,58</button></div>
</div>
</body></html>
"""


def test_juno_secondary_track_number_from_add_to_cart_button():
    results = _parse_web_search_results(
        "Juno",
        "https://www.junodownload.com/search/?q=test",
        JUNO_SECONDARY_HTML_ATC_TRACK,
        artist_q="Test Artist",
        title_q="Test Track",
    )
    assert len(results) == 1
    assert results[0].get("track_number") == "4"


def test_deduplicate_by_url_merges_richer_metadata():
    merged = _deduplicate_by_url([
        {"url": "https://www.junodownload.com/products/a", "title": "A", "artist": "Artist", "score": 100.0, "direct_url": True, "is_fallback": False},
        {"url": "https://www.junodownload.com/products/a", "title": "A", "artist": "Artist", "score": 100.0, "direct_url": True, "is_fallback": False,
         "track_number": "4", "genre": "Funky/Club House", "bpm": "124"},
    ])
    assert len(merged) == 1
    assert merged[0].get("track_number") == "4"
    assert merged[0].get("genre") == "Funky/Club House"
    assert merged[0].get("bpm") == "124"
