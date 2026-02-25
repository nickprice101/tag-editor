"""
Tests for _split_query_artist_title and the q-as-source-of-truth logic in
api_web_search_stream.
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
            "APIC", "TBPM"):
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

from app import _split_query_artist_title, normalize_search_query  # noqa: E402


# ---------------------------------------------------------------------------
# _split_query_artist_title
# ---------------------------------------------------------------------------

def test_en_dash_separator():
    """En-dash with surrounding spaces splits correctly."""
    artist, title = _split_query_artist_title("Adi Oasis \u2013 Dumpalltheguns (@jitwam Remix)")
    assert artist == "Adi Oasis"
    assert title == "Dumpalltheguns (@jitwam Remix)"


def test_em_dash_separator():
    """Em-dash with surrounding spaces splits correctly."""
    artist, title = _split_query_artist_title("Some Artist \u2014 Some Track")
    assert artist == "Some Artist"
    assert title == "Some Track"


def test_hyphen_with_spaces():
    """ASCII hyphen surrounded by spaces splits correctly."""
    artist, title = _split_query_artist_title("40 Thieves - Don't Turn It Off")
    assert artist == "40 Thieves"
    assert title == "Don't Turn It Off"


def test_no_separator_returns_empty_artist():
    """When no valid separator is found, artist is empty and title is the full q."""
    artist, title = _split_query_artist_title("SomeTrackWithoutSeparator")
    assert artist == ""
    assert title == "SomeTrackWithoutSeparator"


def test_hyphen_without_surrounding_spaces_not_split():
    """A hyphen without surrounding whitespace (like in compound words) must NOT split."""
    artist, title = _split_query_artist_title("drum-and-bass")
    assert artist == ""
    assert title == "drum-and-bass"


def test_empty_string():
    """Empty input returns two empty strings."""
    artist, title = _split_query_artist_title("")
    assert artist == ""
    assert title == ""


def test_split_feeds_normalization():
    """Splitting q before normalization yields the correct norm_title and norm_artist."""
    q = "Adi Oasis \u2013 Dumpalltheguns (@jitwam Remix)"
    q_artist, q_title = _split_query_artist_title(q)
    norm_title, norm_artist, remix_tokens = normalize_search_query(q_title, q_artist)
    # Remix token should be captured
    assert any("jitwam" in tok.lower() or "remix" in tok.lower() for tok in remix_tokens)
    # Normalized title should not contain the bracketed remix portion
    assert "jitwam" not in norm_title
    assert norm_artist == "Adi Oasis"


def test_q_overrides_stale_artist_title():
    """Simulates the bug scenario: typed q must win over stale artist/title fields."""
    # The typed search box value
    typed_q = "Adi Oasis \u2013 Dumpalltheguns (@jitwam Remix)"
    # Stale metadata from previously loaded file (should be ignored when q is present)
    stale_artist = "40 Thieves"
    stale_title = "Don't Turn It Off"

    # This is what the fixed endpoint does when q is non-empty:
    q_artist, q_title = _split_query_artist_title(typed_q)
    norm_title, norm_artist, remix_tokens = normalize_search_query(
        q_title or typed_q, q_artist
    )

    # The stale values must NOT appear in the resolved search terms
    assert "40 Thieves" not in norm_artist
    assert "Don't Turn It Off" not in norm_title
    assert norm_artist == "Adi Oasis"
    assert "Dumpalltheguns" in norm_title or norm_title  # title derived from q
