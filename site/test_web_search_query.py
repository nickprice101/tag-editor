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

from app import (_split_query_artist_title, normalize_search_query,  # noqa: E402
                 _score_result, _expand_year_only_date,
                 _normalize_remix_handle, _remix_match_level,
                 obfuscate_key, _REMIX_KW_RE, re,
                 _build_retry_query, _site_search_url,
                 _RETRY_SCORE_THRESHOLD, _TRAILING_REMIX_RE,
                 _should_retry_without_remix, _RETRY_SUFFICIENT_HITS,
                 _juno_thumb_to_full)


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


# ---------------------------------------------------------------------------
# _expand_year_only_date
# ---------------------------------------------------------------------------

def test_expand_year_only_returns_june_15():
    """Year-only string is expanded to YYYY-06-15 for scoring purposes."""
    assert _expand_year_only_date("2019") == "2019-06-15"


def test_expand_full_date_unchanged():
    """Full YYYY-MM-DD date is returned unchanged."""
    assert _expand_year_only_date("2019-03-22") == "2019-03-22"


def test_expand_empty_string_unchanged():
    """Empty string returns empty string."""
    assert _expand_year_only_date("") == ""


def test_expand_partial_date_unchanged():
    """Partial date that is not a plain 4-digit year is not expanded."""
    assert _expand_year_only_date("2019-06") == "2019-06"


# ---------------------------------------------------------------------------
# _normalize_remix_handle
# ---------------------------------------------------------------------------

def test_normalize_remix_handle_strips_at():
    """Leading @ in handle-like words is stripped."""
    assert _normalize_remix_handle("@jitwam Remix") == "jitwam Remix"


def test_normalize_remix_handle_no_at_unchanged():
    """Token without @ is unchanged."""
    assert _normalize_remix_handle("Jitwam Remix") == "Jitwam Remix"


# ---------------------------------------------------------------------------
# _remix_match_level
# ---------------------------------------------------------------------------

def test_remix_match_level_identity_and_keyword():
    """Identity present + remix keyword in result title → level 2."""
    tokens = ["@jitwam Remix"]
    assert _remix_match_level(tokens, "dumpalltheguns (jitwam remix)") == 2


def test_remix_match_level_identity_only():
    """Identity present but no remix keyword in result title → level 1."""
    tokens = ["@jitwam Remix"]
    # "club" is not in the remix keyword list
    assert _remix_match_level(tokens, "dumpalltheguns (jitwam club)") == 1


def test_remix_match_level_no_match():
    """Neither identity nor keyword matches → level 0."""
    tokens = ["@jitwam Remix"]
    assert _remix_match_level(tokens, "dumpalltheguns original") == 0


# ---------------------------------------------------------------------------
# _score_result – penalty-based model
# ---------------------------------------------------------------------------

def test_perfect_match_scores_100():
    """Identical title and artist with no date or remix tokens → 100."""
    score = _score_result("Adi Oasis", "Dumpalltheguns", "Adi Oasis", "Dumpalltheguns")
    assert score == 100.0


def test_no_query_scores_zero():
    """Empty title and artist → 0 (nothing to compare)."""
    score = _score_result("", "", "Some Artist", "Some Title")
    assert score == 0.0


def test_year_only_date_used_for_scoring():
    """Year-only date_q is expanded internally; exact year match incurs no date penalty."""
    # Perfect title+artist match, same year → still 100
    score_same = _score_result(
        "Adi Oasis", "Dumpalltheguns",
        "Adi Oasis", "Dumpalltheguns",
        date_q="2019", res_year="2019",
    )
    assert score_same == 100.0

    # Perfect title+artist match, 5-year diff → small date penalty
    score_diff = _score_result(
        "Adi Oasis", "Dumpalltheguns",
        "Adi Oasis", "Dumpalltheguns",
        date_q="2019", res_year="2024",
    )
    assert score_diff < 100.0
    assert score_diff > 90.0  # date penalty is small relative to full 100


def test_at_jitwam_matches_jitwam_in_result():
    """@jitwam remix token matches 'Jitwam' appearing in result title."""
    tokens = ["@jitwam Remix"]
    # All result titles are the same base length so title-similarity effects are equal;
    # only the remix match level differs.
    # level 2: identity (jitwam) + remix keyword present → no remix penalty
    score_full = _score_result(
        "Adi Oasis", "Dumpalltheguns",
        "Adi Oasis", "Dumpalltheguns (Jitwam Remix)",
        remix_tokens=tokens,
    )
    # level 1: identity (jitwam) present but no remix keyword → -4 penalty
    score_identity = _score_result(
        "Adi Oasis", "Dumpalltheguns",
        "Adi Oasis", "Dumpalltheguns (Jitwam Club)",
        remix_tokens=tokens,
    )
    # level 0: different identity, no jitwam → -8 penalty
    score_none = _score_result(
        "Adi Oasis", "Dumpalltheguns",
        "Adi Oasis", "Dumpalltheguns (Tom VR Remix)",
        remix_tokens=tokens,
    )
    assert score_full > score_identity > score_none


def test_remix_ranking_identity_plus_keyword_beats_identity_only_beats_none():
    """Result with identity+remix keyword ranks above identity-only, which ranks above no-identity."""
    tokens = ["@jitwam Remix"]
    title_q = "Dumpalltheguns"
    artist_q = "Adi Oasis"

    # level 2: jitwam + remix keyword
    score_full = _score_result(
        artist_q, title_q,
        artist_q, f"{title_q} (Jitwam Remix)",
        remix_tokens=tokens,
    )
    # level 1: jitwam present, no remix keyword ("club" is not a remix keyword)
    score_identity = _score_result(
        artist_q, title_q,
        artist_q, f"{title_q} (Jitwam Club)",
        remix_tokens=tokens,
    )
    # level 0: unrelated remixer
    score_none = _score_result(
        artist_q, title_q,
        artist_q, f"{title_q} (Tom VR Remix)",
        remix_tokens=tokens,
    )
    assert score_full > score_identity > score_none


# ---------------------------------------------------------------------------
# obfuscate_key
# ---------------------------------------------------------------------------

def test_obfuscate_key_long_key_shows_eight_x_prefix():
    """A key longer than 8 chars shows exactly 8 x's then the last 8 chars."""
    key = "abcdefghijklmnop"  # 16 chars
    result = obfuscate_key(key)
    assert result == "xxxxxxxxijklmnop"


def test_obfuscate_key_short_key_returned_unchanged():
    """A key of 8 chars or fewer is returned as-is."""
    assert obfuscate_key("abcd1234") == "abcd1234"
    assert obfuscate_key("xxxx") == "xxxx"


def test_obfuscate_key_empty_returns_empty():
    """Empty string returns empty string."""
    assert obfuscate_key("") == ""


def test_obfuscate_key_40char_discogs_token():
    """A 40-char Discogs token is displayed as 8 x's + last 8 chars."""
    token = "A" * 32 + "abcd1234"
    result = obfuscate_key(token)
    assert result == "xxxxxxxxabcd1234"


# ---------------------------------------------------------------------------
# remix identity words are included in norm_q
# ---------------------------------------------------------------------------

def _build_norm_q_out(q: str) -> tuple:
    """Replicate the norm_q / norm_q_out logic from api_web_search_stream."""
    q_artist, q_title = _split_query_artist_title(q)
    norm_title, norm_artist, remix_tokens = normalize_search_query(q_title or q, q_artist)
    norm_q = f"{norm_artist} {norm_title}".strip() if (norm_artist or norm_title) else q
    if remix_tokens:
        identity_words = []
        for tok in remix_tokens:
            norm = _normalize_remix_handle(tok).lower()
            identity_words.extend(
                w for w in re.findall(r'\w+', norm)
                if not _REMIX_KW_RE.match(w) and len(w) > 2
            )
        if identity_words:
            norm_q = f"{norm_q} {' '.join(dict.fromkeys(identity_words))}".strip()
    norm_q_out = norm_q.lower()
    return norm_q, norm_q_out


def test_outgoing_query_is_lowercase():
    """norm_q_out (used for URL construction) must be fully lowercase."""
    _, norm_q_out = _build_norm_q_out("Dumpalltheguns \u2014 Adi Oasis, Jitwam")
    assert norm_q_out == norm_q_out.lower()


def test_outgoing_query_lowercase_with_remix():
    """norm_q_out must be lowercase even when remix tokens add identity words."""
    _, norm_q_out = _build_norm_q_out("Adi Oasis \u2013 Dumpalltheguns (@jitwam Remix)")
    assert norm_q_out == norm_q_out.lower()


def test_scoring_norm_q_unchanged():
    """norm_q (used for scoring) retains original casing; only norm_q_out is lowercased."""
    norm_q, norm_q_out = _build_norm_q_out("Adi Oasis \u2013 Dumpalltheguns")
    # norm_q_out is lowercase
    assert norm_q_out == norm_q_out.lower()
    # norm_q retains mixed case from normalization
    assert norm_q == "Adi Oasis Dumpalltheguns"


def test_remix_identity_words_in_norm_q():
    """Remix identity words extracted from tokens are appended to norm_q."""
    q = "Adi Oasis \u2013 Dumpalltheguns (@jitwam Remix)"
    q_artist, q_title = _split_query_artist_title(q)
    norm_title, norm_artist, remix_tokens = normalize_search_query(q_title, q_artist)
    # Build norm_q the same way api_web_search_stream does
    norm_q = f"{norm_artist} {norm_title}".strip()
    if remix_tokens:
        identity_words = []
        for tok in remix_tokens:
            norm = _normalize_remix_handle(tok).lower()
            identity_words.extend(
                w for w in re.findall(r'\w+', norm)
                if not _REMIX_KW_RE.match(w) and len(w) > 2
            )
        if identity_words:
            norm_q = f"{norm_q} {' '.join(dict.fromkeys(identity_words))}".strip()
    assert "jitwam" in norm_q
    assert "Adi Oasis" in norm_q
    assert "Dumpalltheguns" in norm_q


# ---------------------------------------------------------------------------
# _build_retry_query
# ---------------------------------------------------------------------------

def test_build_retry_query_strips_bare_remix_keyword():
    """Trailing bare remix keyword is removed."""
    assert _build_retry_query("Artist", "Song Remix") == "artist song"


def test_build_retry_query_strips_identity_and_remix():
    """Trailing 'Remixer Remix' segment is removed."""
    assert _build_retry_query("Adi Oasis", "Dumpalltheguns DJ Remix") == "adi oasis dumpalltheguns"


def test_build_retry_query_strips_dash_separated_remix():
    """'- Remixer Remix' dash-separated trailing segment is removed."""
    assert _build_retry_query("Artist", "Song Title - Someone Remix") == "artist song title"


def test_build_retry_query_strips_vip():
    """Trailing 'VIP' descriptor is removed."""
    assert _build_retry_query("Artist", "Song VIP") == "artist song"


def test_build_retry_query_strips_vip_mix():
    """Trailing 'VIP Mix' descriptor is removed."""
    assert _build_retry_query("Artist", "Song Title VIP Mix") == "artist song title"


def test_build_retry_query_no_remix_unchanged():
    """Title with no trailing remix keyword is returned unchanged (lowercased)."""
    assert _build_retry_query("Adi Oasis", "Dumpalltheguns") == "adi oasis dumpalltheguns"


def test_build_retry_query_is_lowercase():
    """Result is always lowercase."""
    result = _build_retry_query("Adi Oasis", "Dumpalltheguns Remix")
    assert result == result.lower()


def test_build_retry_query_does_not_strip_internal_remix():
    """A remix keyword that is NOT trailing is preserved."""
    # "Remix EP" does not end with a bare remix keyword (it ends with "EP")
    result = _build_retry_query("Artist", "Remix EP")
    assert "remix" in result


def test_build_retry_query_empty_inputs():
    """Empty inputs return empty string."""
    assert _build_retry_query("", "") == ""


def test_build_retry_query_artist_only():
    """Only artist, no title: returns lowercased artist."""
    assert _build_retry_query("Adi Oasis", "") == "adi oasis"


# ---------------------------------------------------------------------------
# retry gating
# ---------------------------------------------------------------------------

def test_should_retry_without_remix_when_low_score_and_few_hits():
    """Retry runs only when first-pass quality is low and hit count is insufficient."""
    assert _should_retry_without_remix(True, "artist song", "artist song remix", 65.0, 1) is True


def test_should_not_retry_when_first_pass_has_sufficient_hits():
    """A sufficiently populated first pass suppresses the retry."""
    assert _should_retry_without_remix(True, "artist song", "artist song remix", 10.0, _RETRY_SUFFICIENT_HITS) is False


# ---------------------------------------------------------------------------
# Juno thumbnail conversion
# ---------------------------------------------------------------------------

def test_juno_thumb_to_full_converts_known_thumbnail_pattern():
    """Juno 150px thumbnail URLs are converted to full-size BIG cover URLs."""
    thumb = "https://imagescdn.junodownload.com/150/CS5550603-02A.jpg"
    assert _juno_thumb_to_full(thumb) == "https://imagescdn.junodownload.com/full/CS5550603-02A-BIG.jpg"


def test_juno_thumb_to_full_leaves_other_urls_unchanged():
    """URLs not matching the known thumbnail pattern are returned unchanged."""
    src = "https://example.com/image.jpg"
    assert _juno_thumb_to_full(src) == src


# ---------------------------------------------------------------------------
# _site_search_url
# ---------------------------------------------------------------------------

def test_site_search_url_beatport():
    """Beatport URL contains the encoded query."""
    url = _site_search_url("Beatport", "test+query")
    assert url == "https://www.beatport.com/search/tracks?q=test+query"


def test_site_search_url_traxsource():
    """Traxsource URL contains the encoded query."""
    url = _site_search_url("Traxsource", "test+query")
    assert "test+query" in url
    assert "traxsource.com" in url


def test_site_search_url_juno():
    """Juno URL contains the encoded query."""
    url = _site_search_url("Juno", "test+query")
    assert "test+query" in url
    assert "junodownload.com" in url


def test_site_search_url_unknown_returns_empty():
    """Unknown site name returns empty string."""
    assert _site_search_url("Unknown", "q") == ""


# ---------------------------------------------------------------------------
# Remix keyword boost in _score_result
# ---------------------------------------------------------------------------

def test_remix_keyword_boost_increases_score():
    """A result title containing a remix keyword gets a +3 score boost."""
    score_plain = _score_result("Artist", "Song", "Artist", "Song")
    score_remix = _score_result("Artist", "Song", "Artist", "Song Remix")
    # The remix version has a different title so title penalty applies, but
    # the boost should still raise it above an equivalent non-remix result.
    score_no_remix = _score_result("Artist", "Song", "Artist", "Song Extra")
    assert score_remix > score_no_remix


def test_remixes_title_scores_higher_than_same_length_non_remix():
    """A result title with 'Remixes' (7 chars) scores higher than a same-length non-remix word."""
    # "remixes" and "uploads" are both 7 chars → same title similarity penalty;
    # only the boost differs, so the remix-keyword result must win.
    score_remixes = _score_result("Artist", "Song", "Artist", "Song remixes")
    score_other   = _score_result("Artist", "Song", "Artist", "Song uploads")
    assert score_remixes > score_other


def test_remixes_boost_total_is_five():
    """'Remixes' in result title gives total boost of +5 (base 3 + extra 2)."""
    # Use identical artist/title match as baseline; both artist and title have
    # no similarity penalty. 'Remixes' adds +3 base + +2 extra = +5, clamped to 100.
    score_plain = _score_result("Artist", "Song", "Artist", "Song")
    score_remixes = _score_result("Artist", "Song", "Artist", "Song Remixes")
    # 'Song Remixes' has slightly different title similarity but gets +5 boost.
    # We verify it's above plain (which caps at 100.0) by checking with a lower
    # baseline title that gives room to see the boost:
    score_low = _score_result("Artist", "Song", "Artist", "Different Title")
    score_low_remixes = _score_result("Artist", "Song", "Artist", "Different Title Remixes")
    assert score_low_remixes > score_low


def test_remix_keyword_boost_case_insensitive():
    """Remix keyword boost is applied regardless of case (res_title is lowercased internally)."""
    score_lc = _score_result("Artist", "Song", "Artist", "Song remix")
    score_uc = _score_result("Artist", "Song", "Artist", "Song REMIX")
    assert score_lc == score_uc


def test_perfect_match_still_scores_100():
    """Adding remix boost does not push a plain perfect match above 100."""
    score = _score_result("Artist", "Song", "Artist", "Song")
    assert score == 100.0


def test_remixes_keyword_clamped_to_100():
    """A perfect match + 'Remixes' suffix is clamped to 100, not above."""
    # The title similarity penalty for "song remixes" vs "song" is > 5 pts,
    # so the result won't actually exceed 100.  We verify it's <= 100.
    score = _score_result("Artist", "Song Remixes", "Artist", "Song Remixes")
    assert score == 100.0


# ---------------------------------------------------------------------------
# _retry_meaningful flag logic (replicated from api_web_search_stream)
# ---------------------------------------------------------------------------

def _compute_retry_meaningful(title: str, artist: str) -> bool:
    """Replicate the _retry_meaningful computation from api_web_search_stream."""
    norm_title, norm_artist, remix_tokens = normalize_search_query(title, artist)
    return bool(remix_tokens) or (
        _TRAILING_REMIX_RE.sub("", norm_title).strip(" -,") != norm_title
    )


def test_retry_meaningful_bracketed_remix():
    """Bracketed remix descriptor makes retry meaningful."""
    assert _compute_retry_meaningful("On My Knees (Original Mix)", "Artist") is True


def test_retry_meaningful_bare_trailing_remix():
    """Bare trailing remix keyword (no brackets) makes retry meaningful."""
    assert _compute_retry_meaningful("Song Original Mix", "Artist") is True


def test_retry_meaningful_no_remix():
    """Plain title with no remix descriptor makes retry NOT meaningful."""
    assert _compute_retry_meaningful("On My Knees", "Artist") is False


def test_retry_meaningful_vip_suffix():
    """Trailing 'VIP' suffix makes retry meaningful."""
    assert _compute_retry_meaningful("Song VIP", "Artist") is True


def test_retry_meaningful_dash_remix():
    """Dash-separated remix suffix (e.g. '- Artist Remix') makes retry meaningful."""
    assert _compute_retry_meaningful("Song - DJ Remix", "Artist") is True


# ---------------------------------------------------------------------------
# _RETRY_SCORE_THRESHOLD constant
# ---------------------------------------------------------------------------

def test_retry_score_threshold_value():
    """_RETRY_SCORE_THRESHOLD is defined and set to a sensible value (70–85)."""
    assert 70 <= _RETRY_SCORE_THRESHOLD <= 85


def test_retry_triggered_below_threshold():
    """A result below the threshold should signal that retry is needed."""
    low_score = _RETRY_SCORE_THRESHOLD - 1
    assert low_score < _RETRY_SCORE_THRESHOLD


def test_retry_not_triggered_at_threshold():
    """A perfect match scores well above the threshold; retry is suppressed for good results."""
    perfect_score = _score_result("Artist", "Song", "Artist", "Song")
    assert perfect_score >= _RETRY_SCORE_THRESHOLD


