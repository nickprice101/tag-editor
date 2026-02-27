import os
import re
import base64
import json
import shutil
import threading
import time
from datetime import date as dt_date, datetime as dt_datetime
from io import BytesIO
from difflib import SequenceMatcher
from itertools import islice
from urllib.parse import urlparse, parse_qs, unquote, quote

import requests
import acoustid
from bs4 import BeautifulSoup
from flask import Flask, request, Response, jsonify
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TALB, TPE2, TCON, TRCK, TPUB, COMM, TDRC, TYER,
    TXXX, TSOP, TSO2, APIC, TBPM, TMED, TPOS, TCMP, TDOR, TORY,
    UFID, TIPL, TSSE
)
from mutagen.mp3 import MP3
from PIL import Image

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _sync_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_AVAILABLE = False

APP_USER = os.getenv("APP_USER", "")
APP_PASS = os.getenv("APP_PASS", "")
MUSIC_ROOT = os.getenv("MUSIC_ROOT", "/mnt/HD/HD_a2/Media/Music").rstrip("/")

_BROWSE_STARTUP_CANDIDATE = "/mnt/HD/HD_a2/Media/Music/Downloads/youtube-downloads"
try:
    _real_cand = os.path.realpath(_BROWSE_STARTUP_CANDIDATE)
    _real_root = os.path.realpath(MUSIC_ROOT)
    if (_real_cand.startswith(_real_root + os.sep) or _real_cand == _real_root) and os.path.isdir(_real_cand):
        _BROWSE_DEFAULT = _real_cand
    else:
        _BROWSE_DEFAULT = MUSIC_ROOT
except Exception:
    _BROWSE_DEFAULT = MUSIC_ROOT

DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN", "").strip()
ACOUSTID_KEY = os.getenv("ACOUSTID_KEY", "").strip()
# Set WEB_SEARCH_DEBUG=1 to enable verbose Bandcamp diagnostic SSE logs
# (HTML previews, node snippets).  Off by default to avoid noise.
WEB_SEARCH_DEBUG = os.getenv("WEB_SEARCH_DEBUG", "").strip() == "1"
# Headless browser fallback configuration
HEADLESS_ENABLED = os.getenv("HEADLESS_ENABLED", "1").strip() not in ("0", "false", "no")
HEADLESS_TIMEOUT_SECS = int(os.getenv("HEADLESS_TIMEOUT_SECS", "15"))
HEADLESS_MAX_RESULTS = int(os.getenv("HEADLESS_MAX_RESULTS", "10"))
# Minimum score (0–100) that any non-fallback first-pass result must reach to
# suppress the remix-retry search.  When all structured results score below this
# threshold the retry is triggered even if structured results exist.
_RETRY_SCORE_THRESHOLD = 78
# Treat the first pass as sufficient when it already returns this many
# structured results; in that case skip the remix-stripped retry pass.
_RETRY_SUFFICIENT_HITS = 3

UA = "CorsicanEscapeTagEditor/2.0 (nick@corsicanescape.com)"

# Browser-like User-Agent used specifically for Bandcamp requests to avoid 403s
BANDCAMP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# MusicBrainz Picard-standard TXXX field names
MB_TXXX_FIELDS = [
    "musicbrainz_trackid",
    "musicbrainz_albumid",
    "musicbrainz_releasegroupid",
    "musicbrainz_artistid",
    "musicbrainz_albumartistid",
    "musicbrainz_releasecountry",
    "musicbrainz_releasestatus",
    "musicbrainz_releasetype",
    "musicbrainz_albumtype",
    "musicbrainz_albumstatus",
    "musicbrainz_albumartist",
    "musicbrainz_artist",
    "musicbrainz_album",
    "barcode",
]

# Lightweight tag cache: (path, mtime) -> dict (max 2000 entries)
_tag_cache: dict = {}
_TAG_CACHE_MAX = 2000

app = Flask(__name__)

_TAB_ICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>
<rect width='64' height='64' rx='14' fill='%23FF1A55'/>
<path d='M18 18h28v9H18zm0 14h28v14H18z' fill='white' opacity='.95'/>
<circle cx='24' cy='39' r='3.4' fill='%23FF1A55'/>
<circle cx='32' cy='39' r='3.4' fill='%23FF1A55'/>
<circle cx='40' cy='39' r='3.4' fill='%23FF1A55'/>
</svg>"""
_TAB_ICON_DATA_URL = "data:image/svg+xml," + quote(_TAB_ICON_SVG)

# ---------------- Auth ----------------
def basic_auth_ok() -> bool:
    if not APP_USER and not APP_PASS:
        return True
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        user, pw = raw.split(":", 1)
        return user == APP_USER and pw == APP_PASS
    except Exception:
        return False

def require_basic_auth():
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="mp3-tag-editor"'})

# ---------------- Safety ----------------
def safe_path(p: str) -> str:
    p = (p or "").strip()
    if not p:
        raise ValueError("File path is required.")
    real = os.path.realpath(p)
    root = os.path.realpath(MUSIC_ROOT)
    if not real.startswith(root + os.sep) and real != root:
        raise ValueError(f"Path must be under {MUSIC_ROOT}")
    if not os.path.isfile(real):
        raise ValueError("File does not exist (or is not a file).")
    if not real.lower().endswith(".mp3"):
        raise ValueError("Only .mp3 files are supported.")
    return real

def safe_dir(d: str) -> str:
    d = (d or "").strip() or MUSIC_ROOT
    real = os.path.realpath(d)
    root = os.path.realpath(MUSIC_ROOT)
    if not real.startswith(root + os.sep) and real != root:
        raise ValueError(f"Directory must be under {MUSIC_ROOT}")
    if not os.path.isdir(real):
        raise ValueError("Directory does not exist.")
    return real

# ---------------- Helpers ----------------
def normalize_involved_people(s: str) -> str:
    parts = [p.strip() for p in (s or "").split(",")]
    parts = [p for p in parts if p]
    return ", ".join(parts)

def sort_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    if name.lower().startswith("the "):
        rest = name[4:].strip()
        return f"{rest}, The" if rest else name
    return name

def obfuscate_key(key: str) -> str:
    """Show 8 'x' prefix then the last 8 chars, or full key if ≤ 8 chars."""
    if not key:
        return ""
    if len(key) <= 8:
        return key
    return "xxxxxxxx" + key[-8:]

def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

# Patterns for query normalisation
_BRACKET_RE = re.compile(r'\(([^)]*)\)|\[([^\]]*)\]')
_REMIX_KW_RE = re.compile(r'\b(?:remix|mix|edit|vip|version|bootleg|rework|flip|dub|instrumental|acappella|mashup)\b', re.IGNORECASE)
# Matches a trailing remix descriptor in a normalised title: an optional dash/em-dash
# separator followed by one-or-more words ending with a remix keyword, e.g.
#   " - Artist Remix"  →  removes " - Artist Remix"
#   " VIP Mix"         →  removes " VIP Mix"
_TRAILING_REMIX_RE = re.compile(
    r'(?:'
    r'\s*[-\u2013\u2014]\s*\w[\w\s]*\b(?:mix|remix|vip|edit|version|bootleg|rework|flip|dub|instrumental|acappella|mashup)\b'
    r'|\s+(?:\w+\s+)?(?:mix|remix|vip|edit|version|bootleg|rework|flip|dub|instrumental|acappella|mashup)\b'
    r')\s*$',
    re.IGNORECASE,
)
_FEAT_RE = re.compile(
    r'\s*[\(\[]?(?:feat(?:uring)?|ft)\.?\s+[^\)\]]+[\)\]]?'
    r'|\s+(?:with|w/)\s+[^,\(\[]+',
    re.IGNORECASE,
)

def normalize_search_query(title: str, artist: str = "") -> tuple:
    """Strip bracketed content, feat segments and extract remix tokens for search.

    Returns (clean_title, clean_artist, remix_tokens) where remix_tokens is a
    list of bracket-content strings that look like remix/edit descriptors.
    These are stripped from the query so searches stay broad, but can be used
    as a secondary scoring signal.
    """
    remix_tokens: list = []

    # Collect remix tokens from brackets in the title before removing them
    for m in _BRACKET_RE.finditer(title):
        content = (m.group(1) or m.group(2) or "").strip()
        if _REMIX_KW_RE.search(content):
            remix_tokens.append(content)

    # Strip all bracketed content from title
    clean_title = _BRACKET_RE.sub(" ", title)
    # Remove feat segments from title and artist
    clean_title = _FEAT_RE.sub("", clean_title)
    clean_artist = _FEAT_RE.sub("", artist)

    # Normalize whitespace and trailing punctuation
    clean_title = re.sub(r'\s+', ' ', clean_title).strip(" -,")
    clean_artist = re.sub(r'\s+', ' ', clean_artist).strip(" -,")

    return clean_title, clean_artist, remix_tokens


def _build_retry_query(norm_artist: str, norm_title: str) -> str:
    """Build a stripped search query for per-site remix retry.

    Used when a store search yields zero structured results: removes trailing
    remix descriptors from *norm_title* (e.g. '- Artist Remix', 'VIP Mix') so
    the retry casts a broader net.  Identity words are intentionally NOT
    appended; this returns the plain ``artist title`` lowercase string.

    Returns an empty string if both inputs are empty.
    """
    stripped = _TRAILING_REMIX_RE.sub("", norm_title).strip(" -,")
    return f"{norm_artist} {stripped}".strip().lower()


def _site_search_url(site_name: str, qq: str) -> str:
    """Return the search URL for a named store with an encoded query string."""
    if site_name == "Beatport":
        return f"https://www.beatport.com/search/tracks?q={qq}"
    if site_name == "Traxsource":
        return f"https://www.traxsource.com/search?term={qq}&page=1&type=tracks"
    if site_name == "Juno":
        return f"https://www.junodownload.com/search/?solrorder=relevancy&q%5Btitle%5D%5B0%5D={qq}"
    return ""


def _juno_thumb_to_full(url: str) -> str:
    """Convert Juno CDN thumbnail URLs to their full-size cover URLs."""
    if not url:
        return ""
    return re.sub(r"/150/([A-Za-z0-9-]+)\.jpg$", r"/full/\1-BIG.jpg", url)


def _bandcamp_thumb_to_full(url: str) -> str:
    """Convert Bandcamp result thumbnails (_7/_8/etc) to large (_16) URLs."""
    if not url:
        return ""
    return re.sub(r"_(?:\d+)\.jpg$", "_16.jpg", url)


def _should_retry_without_remix(retry_meaningful: bool, retry_q_out: str, norm_q_out: str,
                                first_pass_best_score: float, first_pass_hit_count: int) -> bool:
    """Decide whether the remix-stripped retry search should run.

    Retry only when the first pass is both *low quality* (best score under the
    threshold) and *insufficiently populated* (fewer than the sufficient-hit
    threshold).
    """
    if not retry_meaningful or not retry_q_out or retry_q_out == norm_q_out:
        return False
    if first_pass_hit_count >= _RETRY_SUFFICIENT_HITS:
        return False
    return first_pass_best_score < _RETRY_SCORE_THRESHOLD


def _split_query_artist_title(q: str) -> tuple:
    """Split a free-form query string into (artist, title) when it matches
    "Artist – Title" or "Artist - Title" (en-dash, em-dash, or ASCII hyphen
    surrounded by whitespace).  Returns ("", q) when no separator is found so
    the caller can treat the whole string as the title / full query.
    """
    m = re.search(r'\s+[\u2013\u2014]\s+|\s+-\s+', q)
    if m:
        artist = q[:m.start()].strip()
        title = q[m.end():].strip()
        if artist and title:
            return artist, title
    return "", q


def _expand_year_only_date(date_str: str) -> str:
    """Expand a year-only string (YYYY) to YYYY-06-15 for date comparison.
    Returns the input unchanged if it already contains month/day or is empty."""
    if date_str and re.match(r'^\d{4}$', date_str.strip()):
        return date_str.strip() + "-06-15"
    return date_str or ""


def _normalize_remix_handle(token: str) -> str:
    """Strip leading @ from handle-like words (e.g. '@jitwam Remix' -> 'jitwam Remix')."""
    return re.sub(r'(?<!\w)@(\w)', r'\1', token)


def _remix_match_level(remix_tokens: list, res_title_lc: str) -> int:
    """Return the best remix match level for the result title.

    Returns:
        2  – at least one token's identity is present AND a remix keyword is present
        1  – at least one token's identity is present (no remix keyword)
        0  – no identity match found
    """
    for token in remix_tokens:
        norm = _normalize_remix_handle(token).lower()
        # Extract non-keyword words as the remixer identity
        identity_words = [
            w for w in re.findall(r'\w+', norm)
            if not _REMIX_KW_RE.match(w) and len(w) > 2
        ]
        if identity_words and any(w in res_title_lc for w in identity_words):
            if _REMIX_KW_RE.search(res_title_lc):
                return 2
            return 1
    return 0


def _score_result(artist_q: str, title_q: str, res_artist: str, res_title: str,
                  year_q: str = "", res_year: str = "", remix_tokens: list = None,
                  date_q: str = "") -> float:
    """Score 0–100 (penalty-based). Start at 100, subtract for mismatches.

    Penalties:
      - Title:  up to 60 pts  (weighted by 1 − similarity)
      - Artist: up to 40 pts  (weighted by 1 − similarity; @ handles normalised)
      - Date:   up to 10 pts  (full-date distance) or up to 5 pts (year-only)
      - Remix:  up to 8 pts   (identity missing) or 4 pts (identity present, no keyword)
    """
    if not title_q and not artist_q:
        return 0.0

    # Normalise case centrally so all comparisons are case-insensitive
    title_q = (title_q or "").lower()
    res_title = (res_title or "").lower()
    artist_q = (artist_q or "").lower()
    res_artist = (res_artist or "").lower()

    score = 100.0

    # Title penalty (up to 60 pts)
    if title_q:
        score -= (1.0 - _similarity(title_q, res_title)) * 60

    # Artist penalty (up to 40 pts); normalise leading @ on both sides
    if artist_q:
        norm_aq = _normalize_remix_handle(artist_q)
        norm_ra = _normalize_remix_handle(res_artist or "")
        score -= (1.0 - _similarity(norm_aq, norm_ra)) * 40

    # Date penalty
    effective_date_q = _expand_year_only_date(date_q) if date_q else ""
    if effective_date_q:
        # Full date (or expanded year-only) available for query
        res_date_str = _expand_year_only_date(res_year) if res_year else ""
        if res_date_str:
            try:
                q_d = dt_date.fromisoformat(effective_date_q[:10])
                r_d = dt_date.fromisoformat(res_date_str[:10])
                diff_days = abs((q_d - r_d).days)
                max_days = 10 * 365.25
                score -= min(1.0, diff_days / max_days) * 10
            except (ValueError, TypeError):
                pass
    elif year_q and res_year and year_q.isdigit() and res_year.isdigit():
        # Year-only fallback: small penalty scaled over 10 years
        diff = abs(int(year_q) - int(res_year))
        score -= min(1.0, diff / 10.0) * 5

    # Remix penalty: penalise when remix intent from query is unmet
    if remix_tokens and res_title:
        level = _remix_match_level(remix_tokens, res_title.lower())
        if level == 0:
            score -= 8
        elif level == 1:
            score -= 4

    # Remix keyword boost: small additive reward for results whose title
    # contains remix-related terms.  "Remixes" gets an extra bump to help
    # compilation pages surface reliably in retry searches.
    if res_title:
        _has_remixes = bool(re.search(r'\bremixes\b', res_title))
        if _REMIX_KW_RE.search(res_title) or _has_remixes:
            score += 3
        if _has_remixes:
            score += 2

    return round(min(100.0, max(0.0, score)), 1)


def _beatport_date_proximity_score(date_q: str, release_date: str) -> tuple:
    """Return (proximity_score, unlocks_perfect) for Beatport date scoring.

    Compares the query date (date_q, YYYY-MM-DD) against Beatport's release_date
    (YYYY-MM-DD) and returns a continuous real-valued score in [0.0, 10.0].

    Proximity mapping:
        0 days      → 10.0 points, unlocks_perfect = True
        Linear decrease to 0.0 at 10 years (10 * 365.25 days); clamped at 0.

    If either date is missing or unparsable returns (0.0, False).
    """
    if not date_q or not release_date:
        return (0.0, False)
    try:
        # Accept YYYY-MM-DD; ignore time component if present
        q_str = date_q[:10]
        r_str = release_date[:10]
        q_d = dt_date.fromisoformat(q_str)
        r_d = dt_date.fromisoformat(r_str)
        diff = abs((q_d - r_d).days)
        max_days = 10 * 365.25
        proximity = max(0.0, 10.0 * (1.0 - diff / max_days))
        unlocks_perfect = (diff == 0)
        return (proximity, unlocks_perfect)
    except (ValueError, TypeError):
        return (0.0, False)


_COMPILATION_ARTIST_RE = re.compile(
    r'\bvarious(\s+artists?)?\b|\bv\.?/a\.?\b', re.IGNORECASE
)

_BANDCAMP_SCORE_BOOST = 3  # small preference boost for Bandcamp results




def _compilation_penalty(artist: str, release_type: str = "", track_count: int = 0) -> float:
    """Returns a penalty (negative float) when a result is detected as a compilation.

    Rules:
    - If artist matches a V/A pattern OR release_type contains 'compilation': -15 points.
      - Additional -5 if track_count > 20, or -2 if track_count > 10.
    - If not detected as compilation but track_count is very large (>30): -5 points.
    - If nothing detected, returns 0 (no penalty).
    """
    norm = (artist or "").strip()
    is_comp = (
        bool(_COMPILATION_ARTIST_RE.search(norm))
        or norm.lower() == "va"
        or "compilation" in (release_type or "").lower()
    )
    if is_comp:
        penalty = -15.0
        if track_count > 20:
            penalty -= 5.0
        elif track_count > 10:
            penalty -= 2.0
        return penalty
    if track_count > 30:
        return -5.0
    return 0.0

def _normalize_tag(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for genre comparison."""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _genre_folders() -> list:
    """Return sorted immediate subdirectory names under MUSIC_ROOT, excluding Downloads."""
    try:
        root = os.path.realpath(MUSIC_ROOT)
        folders = []
        with os.scandir(root) as it:
            for e in it:
                if (e.is_dir(follow_symlinks=False)
                        and not e.name.startswith(".")
                        and e.name.lower() != "downloads"):
                    folders.append(e.name)
        folders.sort(key=str.lower)
        return folders
    except Exception:
        return []

def _map_tags_to_folders(mb_tags: list, folders: list, threshold: float = 0.72) -> list:
    """Map MusicBrainz tag names to MUSIC_ROOT folder names.
    Returns list of match dicts sorted by quality (exact first, then score desc)."""
    if not mb_tags or not folders:
        return []
    norm_folders = [(f, _normalize_tag(f)) for f in folders]
    results = []
    seen: set = set()
    for tag in mb_tags:
        tag_name = tag.get("name", "")
        if not tag_name:
            continue
        norm_t = _normalize_tag(tag_name)
        best_folder, best_score, exact = None, 0.0, False
        for folder, norm_f in norm_folders:
            if norm_f == norm_t:
                best_folder, best_score, exact = folder, 1.0, True
                break
            s = SequenceMatcher(None, norm_t, norm_f).ratio()
            if s > best_score:
                best_score, best_folder = s, folder
        if best_folder and (exact or best_score >= threshold) and best_folder not in seen:
            seen.add(best_folder)
            results.append({
                "folder": best_folder,
                "mb_tag": tag_name,
                "score": round(best_score, 3),
                "exact": exact,
                "count": tag.get("count", 0),
            })
    results.sort(key=lambda x: (not x["exact"], -x["score"], -x.get("count", 0)))
    return results

def sanitize_component(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "Unknown"
    s = s.replace("/", "-").replace("\\", "-").replace("\0", "")
    s = re.sub(r'[:*"<>?|]', "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:180]

def get_text(tags: ID3, key: str) -> str:
    f = tags.get(key)
    if not f or not getattr(f, "text", None):
        return ""
    return str(f.text[0]).strip()

def get_txxx(tags: ID3, desc: str) -> str:
    for t in tags.getall("TXXX"):
        if getattr(t, "desc", "") == desc and getattr(t, "text", None):
            return str(t.text[0]).strip()
    return ""

def set_txxx(tags: ID3, desc: str, value: str):
    value = (value or "").strip()
    if not value:
        # remove existing if blank
        tags.setall("TXXX", [t for t in tags.getall("TXXX") if t.desc != desc])
        return
    tags.setall(
        "TXXX",
        [t for t in tags.getall("TXXX") if t.desc != desc] + [TXXX(encoding=3, desc=desc, text=[value])]
    )

def extract_year(tags: ID3) -> str:
    y = get_text(tags, "TYER")
    if y and y[:4].isdigit():
        return y[:4]
    d = get_text(tags, "TDRC")
    if d and d[:4].isdigit():
        return d[:4]
    y2 = get_txxx(tags, "year")
    if y2 and y2[:4].isdigit():
        return y2[:4]
    return ""

def set_text_frame(tags, frame_cls, text: str):
    if text is None:
        return
    text = str(text).strip()
    if text == "":
        return
    tags.setall(frame_cls.__name__, [frame_cls(encoding=3, text=[text])])

def http_get(url: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    return requests.get(url, headers=headers, **kwargs)

def bandcamp_get(url: str, **kwargs):
    """GET helper for Bandcamp that sends browser-like headers to avoid 403s.

    This intentionally mirrors a normal top-level browser navigation request so
    Bandcamp receives familiar fetch metadata during the first pass.
    """
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", BANDCAMP_UA)
    headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    headers.setdefault("Accept-Encoding", "gzip, deflate, br")
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    headers.setdefault("Referer", "https://bandcamp.com/")
    headers.setdefault("Origin", "https://bandcamp.com")
    headers.setdefault("DNT", "1")
    headers.setdefault("Upgrade-Insecure-Requests", "1")
    headers.setdefault("Sec-Fetch-Dest", "document")
    headers.setdefault("Sec-Fetch-Mode", "navigate")
    headers.setdefault("Sec-Fetch-Site", "same-origin")
    headers.setdefault("Sec-Fetch-User", "?1")
    headers.setdefault("sec-ch-ua", '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"')
    headers.setdefault("sec-ch-ua-mobile", "?0")
    headers.setdefault("sec-ch-ua-platform", '"Windows"')
    headers.setdefault("Cache-Control", "no-cache")
    headers.setdefault("Pragma", "no-cache")
    return requests.get(url, headers=headers, **kwargs)

def _headless_get_html(url: str, timeout_secs: int = None, *, _browser=None) -> str:
    """Fetch a fully-rendered page using headless Chromium via Playwright.

    When *_browser* is a Playwright Browser instance it is reused (caller is
    responsible for its lifecycle).  When None, a new browser is launched and
    closed automatically.

    Raises RuntimeError if Playwright is not installed.
    """
    if timeout_secs is None:
        timeout_secs = HEADLESS_TIMEOUT_SECS
    if not _PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright not installed; run: pip install playwright && playwright install chromium"
        )
    own_browser = _browser is None
    pw = None
    if own_browser:
        pw = _sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    else:
        browser = _browser
    context = browser.new_context(
        user_agent=BANDCAMP_UA,
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        page = context.new_page()
        try:
            page.goto(url, timeout=timeout_secs * 1000, wait_until="domcontentloaded")
            return page.content()
        finally:
            page.close()
    finally:
        context.close()
        if own_browser:
            browser.close()
            if pw is not None:
                pw.stop()


def quick_tags(path: str) -> dict:
    """Lightweight metadata extraction cached by (path, mtime)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    key = (path, mtime)
    if key in _tag_cache:
        return _tag_cache[key]
    result = {}
    try:
        tags = ID3(path)
        result = {
            "artist": get_text(tags, "TPE1"),
            "title": get_text(tags, "TIT2"),
            "date": get_text(tags, "TDRC") or extract_year(tags),
            "genre": get_text(tags, "TCON"),
            "has_art": bool(tags.getall("APIC")),
        }
    except Exception:
        result = {"artist": "", "title": "", "date": "", "genre": "", "has_art": False}
    _tag_cache[key] = result
    if len(_tag_cache) > _TAG_CACHE_MAX:
        # Evict the oldest quarter of entries
        for old_key in list(_tag_cache)[:_TAG_CACHE_MAX // 4]:
            _tag_cache.pop(old_key, None)
    return result

# ---------------- ID3 write/read ----------------
def upsert_id3(mp3_path: str, fields: dict):
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    set_text_frame(tags, TIT2, fields.get("title"))
    set_text_frame(tags, TPE1, fields.get("artist"))
    set_text_frame(tags, TALB, fields.get("album"))
    set_text_frame(tags, TPE2, fields.get("albumartist"))
    set_text_frame(tags, TCON, fields.get("genre"))
    set_text_frame(tags, TRCK, fields.get("track"))
    set_text_frame(tags, TPUB, fields.get("publisher"))
    if "media_type" in fields:
        set_text_frame(tags, TMED, fields.get("media_type"))
    if "part_of_a_set" in fields:
        set_text_frame(tags, TPOS, fields.get("part_of_a_set"))
    if "part_of_a_compilation" in fields:
        set_text_frame(tags, TCMP, fields.get("part_of_a_compilation"))
    if "encoder_settings" in fields:
        set_text_frame(tags, TSSE, fields.get("encoder_settings"))

    bpm_raw = (fields.get("bpm") or "").strip()
    if bpm_raw:
        try:
            bpm_int = str(int(float(bpm_raw)))
            tags.setall("TBPM", [TBPM(encoding=3, text=[bpm_int])])
        except (ValueError, TypeError):
            tags.delall("TBPM")
    else:
        tags.delall("TBPM")

    comment = (fields.get("comment") or "").strip()
    if comment:
        tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=[comment])])
    else:
        tags.delall("COMM")

    date = (fields.get("date") or "").strip()
    year = (fields.get("year") or "").strip()
    original_year = (fields.get("original_year") or "").strip()

    if date:
        tags.setall("TDRC", [TDRC(encoding=3, text=[date])])
    else:
        tags.delall("TDRC")

    if year:
        tags.setall("TYER", [TYER(encoding=3, text=[year])])
        set_txxx(tags, "year", year)
    else:
        tags.delall("TYER")
        set_txxx(tags, "year", "")

    set_txxx(tags, "original_year", original_year)
    if original_year:
        tags.setall("TDOR", [TDOR(encoding=3, text=[original_year])])
        tags.setall("TORY", [TORY(encoding=3, text=[original_year])])
    else:
        tags.delall("TDOR")
        tags.delall("TORY")

    involved = normalize_involved_people(fields.get("involved_people_list") or "")
    set_txxx(tags, "involved_people_list", involved)
    if involved:
        tags.setall("TIPL", [TIPL(encoding=3, text=[involved])])
    else:
        tags.delall("TIPL")

    artist_sort = (fields.get("artist_sort") or "").strip()
    albumartist_sort = (fields.get("albumartist_sort") or "").strip()
    if artist_sort:
        tags.setall("TSOP", [TSOP(encoding=3, text=[artist_sort])])
    else:
        tags.delall("TSOP")
    if albumartist_sort:
        tags.setall("TSO2", [TSO2(encoding=3, text=[albumartist_sort])])
    else:
        tags.delall("TSO2")

    if "unique_file_identifier" in fields:
        unique_file_identifier = (fields.get("unique_file_identifier") or "").strip()
        if unique_file_identifier:
            tags.setall("UFID", [UFID(owner="StarbuckRadio", data=unique_file_identifier.encode("utf-8"))])
        else:
            tags.delall("UFID")

    # extra fields
    if "label" in fields:
        set_txxx(tags, "label", fields.get("label", ""))
    set_txxx(tags, "CATALOGNUMBER", fields.get("catalog_number", ""))
    set_txxx(tags, "catalog_number", "")

    # MusicBrainz Picard TXXX fields
    for mb_key in MB_TXXX_FIELDS:
        set_txxx(tags, mb_key, fields.get(mb_key, ""))

    art_url = (fields.get("art_url") or "").strip()
    if art_url:
        r = http_get(art_url, timeout=25)
        r.raise_for_status()
        im = Image.open(BytesIO(r.content)).convert("RGB")
        out = BytesIO()
        im.save(out, format="JPEG", quality=92)
        jpg = out.getvalue()
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpg))

    tags.save(mp3_path, v2_version=3)  # ID3v2.3

def read_tags_and_audio(mp3_path: str) -> dict:
    tags = ID3(mp3_path)
    mp3 = MP3(mp3_path)

    comm = ""
    if tags.getall("COMM") and tags.getall("COMM")[0].text:
        comm = str(tags.getall("COMM")[0].text[0])

    ufid = ""
    for frame in tags.getall("UFID"):
        if getattr(frame, "owner", "") == "StarbuckRadio":
            ufid = frame.data.decode("utf-8", errors="ignore").strip()
            break
    if not ufid and tags.getall("UFID"):
        ufid = tags.getall("UFID")[0].data.decode("utf-8", errors="ignore").strip()

    return {
        "path": mp3_path,
        "title": get_text(tags, "TIT2"),
        "artist": get_text(tags, "TPE1"),
        "album": get_text(tags, "TALB"),
        "albumartist": get_text(tags, "TPE2"),
        "genre": get_text(tags, "TCON"),
        "track": get_text(tags, "TRCK"),
        "publisher": get_text(tags, "TPUB"),
        "comment": comm,
        "date": get_text(tags, "TDRC"),
        "year": extract_year(tags),
        "original_year": get_text(tags, "TDOR") or get_text(tags, "TORY") or get_txxx(tags, "original_year"),
        "artist_sort": get_text(tags, "TSOP"),
        "albumartist_sort": get_text(tags, "TSO2"),
        "media_type": get_text(tags, "TMED"),
        "part_of_a_set": get_text(tags, "TPOS"),
        "part_of_a_compilation": get_text(tags, "TCMP"),
        "unique_file_identifier": ufid,
        "encoder_settings": get_text(tags, "TSSE"),
        "involved": get_text(tags, "TIPL"),
        "involved_people_list": get_txxx(tags, "involved_people_list"),
        "label": get_txxx(tags, "label"),
        "catalog_number": get_txxx(tags, "CATALOGNUMBER") or get_txxx(tags, "catalog_number"),
        "bpm": get_text(tags, "TBPM"),
        "has_art": bool(tags.getall("APIC")),
        "length_seconds": float(getattr(mp3.info, "length", 0.0) or 0.0),
        "bitrate_kbps": int((getattr(mp3.info, "bitrate", 0) or 0) / 1000),
        "sample_rate_hz": int(getattr(mp3.info, "sample_rate", 0) or 0),
        **{mb_key: get_txxx(tags, mb_key) for mb_key in MB_TXXX_FIELDS},
    }

# ---------------- Archive ----------------
def archive_mp3(mp3_path: str) -> str:
    tags = ID3(mp3_path)
    genre = sanitize_component(get_text(tags, "TCON"))
    albumartist = sanitize_component(get_text(tags, "TPE2") or get_text(tags, "TPE1"))
    album = sanitize_component(get_text(tags, "TALB"))
    title = sanitize_component(get_text(tags, "TIT2"))
    year = extract_year(tags)
    album_dir = f"{album} [{year}]" if year else album

    track_raw = get_text(tags, "TRCK")
    track = track_raw.split("/")[0].strip() if track_raw else ""
    track = track.zfill(2) if track.isdigit() else (sanitize_component(track) if track else "00")

    dest_dir = os.path.join(MUSIC_ROOT, genre, albumartist, album_dir)
    os.makedirs(dest_dir, exist_ok=True)

    base = f"{track} - {title}.mp3"
    dest = os.path.join(dest_dir, base)

    if os.path.exists(dest) and os.path.realpath(dest) != os.path.realpath(mp3_path):
        root, ext = os.path.splitext(dest)
        i = 2
        while os.path.exists(f"{root} ({i}){ext}"):
            i += 1
        dest = f"{root} ({i}){ext}"

    shutil.move(mp3_path, dest)
    return dest

# ---------------- File browsing/search ----------------
def list_dir(dir_path: str, q: str = "", limit: int = 200):
    dir_path = safe_dir(dir_path)
    q = (q or "").strip().lower()

    entries = []
    with os.scandir(dir_path) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            full = os.path.join(dir_path, e.name)
            rel = os.path.relpath(full, MUSIC_ROOT)
            if q and q not in e.name.lower() and q not in rel.lower():
                continue
            if e.is_dir(follow_symlinks=False):
                entries.append({"type": "dir", "name": e.name, "path": full, "rel": rel})
            elif e.is_file(follow_symlinks=False) and e.name.lower().endswith(".mp3"):
                meta = quick_tags(full)
                entries.append({"type": "file", "name": e.name, "path": full, "rel": rel, **meta})
    # dirs first, then files
    entries.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))
    return entries[:limit]

def search_files(root_dir: str, q: str, limit: int = 200):
    root_dir = safe_dir(root_dir)
    q = (q or "").strip().lower()
    if not q:
        return []
    results = []
    for base, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if not fn.lower().endswith(".mp3"):
                continue
            full = os.path.join(base, fn)
            rel = os.path.relpath(full, MUSIC_ROOT)
            if q in fn.lower() or q in rel.lower():
                meta = quick_tags(full)
                results.append({"type": "file", "name": fn, "path": full, "rel": rel, **meta})
                if len(results) >= limit:
                    return results
    return results

# ---------------- Lookup helpers ----------------
def mb_headers():
    return {"User-Agent": UA}

_mb_last_req_time: float = 0.0
_mb_throttle_lock = threading.Lock()
_MB_THROTTLE_SECS = 1.0
_cover_art_exists_cache: dict = {}


def _cover_art_urls_if_exists(release_id: str) -> tuple:
    """Return (thumb, full) cover art URLs only when a front image exists."""
    rid = (release_id or "").strip()
    if not rid:
        return "", ""
    cached = _cover_art_exists_cache.get(rid)
    if cached is not None:
        return cached

    thumb_url = f"https://coverartarchive.org/release/{rid}/front-250"
    full_url = f"https://coverartarchive.org/release/{rid}/front"
    try:
        r = requests.head(thumb_url, headers=mb_headers(), timeout=10, allow_redirects=True)
        ok = r.status_code < 400 and "image" in (r.headers.get("content-type") or "").lower()
    except Exception:
        ok = False

    result = (thumb_url, full_url) if ok else ("", "")
    _cover_art_exists_cache[rid] = result
    return result

def mb_get(url: str, **kwargs) -> requests.Response:
    """MusicBrainz HTTP GET: ~1s throttle (best-effort) + retry on errors/429/5xx."""
    global _mb_last_req_time
    kwargs.setdefault("timeout", 25)
    kwargs.setdefault("headers", mb_headers())
    max_attempts = 3
    last_exc = None
    for attempt in range(max_attempts):
        with _mb_throttle_lock:
            elapsed = time.monotonic() - _mb_last_req_time
            if elapsed < _MB_THROTTLE_SECS:
                time.sleep(_MB_THROTTLE_SECS - elapsed)
            _mb_last_req_time = time.monotonic()
        try:
            r = requests.get(url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                time.sleep(2.0 ** attempt)
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 2 ** (attempt + 1)))
            time.sleep(wait)
            continue
        if r.status_code >= 500 and attempt < max_attempts - 1:
            time.sleep(2.0 ** attempt)
            continue
        return r
    if last_exc is not None:
        raise last_exc
    raise requests.RequestException(f"MusicBrainz request failed after {max_attempts} attempts")

@app.route("/api/load", methods=["GET"])
def api_load():
    if not basic_auth_ok():
        return require_basic_auth()
    try:
        path = safe_path(request.args.get("path", ""))
        return jsonify(read_tags_and_audio(path))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/list", methods=["GET"])
def api_list():
    if not basic_auth_ok():
        return require_basic_auth()
    try:
        d = request.args.get("dir", MUSIC_ROOT)
        q = request.args.get("q", "")
        return jsonify({"dir": safe_dir(d), "items": list_dir(d, q=q)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/ping", methods=["GET"])
def api_ping():
    return jsonify({"ok": True})

@app.route("/api/search", methods=["GET"])
def api_search():
    if not basic_auth_ok():
        return require_basic_auth()
    try:
        d = request.args.get("dir", MUSIC_ROOT)
        q = request.args.get("q", "")
        return jsonify({"dir": safe_dir(d), "results": search_files(d, q=q)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/art", methods=["GET"])
def api_art():
    if not basic_auth_ok():
        return require_basic_auth()
    try:
        path = safe_path(request.args.get("path", ""))
        full = request.args.get("full", "0") == "1"
        tags = ID3(path)
        pics = tags.getall("APIC")
        if not pics:
            return Response(status=404)
        data = pics[0].data
        im = Image.open(BytesIO(data)).convert("RGB")
        if not full:
            im.thumbnail((80, 80), Image.LANCZOS)
        out = BytesIO()
        quality = 90 if full else 75
        im.save(out, format="JPEG", quality=quality)
        return Response(out.getvalue(), mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store, max-age=0"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/art_meta", methods=["GET"])
def api_art_meta():
    if not basic_auth_ok():
        return require_basic_auth()
    try:
        path = safe_path(request.args.get("path", ""))
        tags = ID3(path)
        pics = tags.getall("APIC")
        if not pics:
            return jsonify({"has_art": False})
        im = Image.open(BytesIO(pics[0].data))
        w, h = im.size
        return jsonify({"has_art": True, "width": w, "height": h})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/key_status", methods=["GET"])
def api_key_status():
    if not basic_auth_ok():
        return require_basic_auth()
    return jsonify({
        "discogs": {
            "present": bool(DISCOGS_TOKEN),
            "display": f"DISCOGS_TOKEN = {obfuscate_key(DISCOGS_TOKEN)}" if DISCOGS_TOKEN else "DISCOGS_TOKEN not set",
        },
        "acoustid": {
            "present": bool(ACOUSTID_KEY),
            "display": f"ACOUSTID_KEY = {obfuscate_key(ACOUSTID_KEY)}" if ACOUSTID_KEY else "ACOUSTID_KEY not set",
        },
    })

@app.route("/api/url_dim", methods=["GET"])
def api_url_dim():
    if not basic_auth_ok():
        return require_basic_auth()
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Provide a URL."}), 400
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return jsonify({"error": "Only http/https URLs are supported."}), 400
    try:
        r = http_get(url, timeout=15)
        r.raise_for_status()
        im = Image.open(BytesIO(r.content))
        w, h = im.size
        return jsonify({"width": w, "height": h})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ----- MusicBrainz -----
@app.route("/api/mb_search", methods=["GET"])
def api_mb_search():
    if not basic_auth_ok():
        return require_basic_auth()
    title = (request.args.get("title") or "").strip()
    artist = (request.args.get("artist") or "").strip()
    album = (request.args.get("album") or "").strip()
    year = (request.args.get("year") or "").strip()
    if not title and not artist:
        return jsonify({"error": "Provide at least a title or artist."}), 400

    norm_title, norm_artist, _ = normalize_search_query(title, artist)
    parts = []
    if norm_title:
        parts.append(f'recording:"{norm_title}"')
    if norm_artist:
        parts.append(f'artist:"{norm_artist}"')
    if album:
        parts.append(f'release:"{album}"')
    if year and year.isdigit():
        parts.append(f'date:{year}')
    q = " AND ".join(parts)

    r = mb_get("https://musicbrainz.org/ws/2/recording",
                     params={"query": q, "fmt": "json", "limit": 10})
    r.raise_for_status()
    data = r.json()
    out = []
    for rec in data.get("recordings", []):
        ac = rec.get("artist-credit", [])
        artist_name = ac[0].get("name") if ac else ""
        releases = rec.get("releases", []) or []
        album = ""
        albumartist = ""
        if releases:
            rel = releases[0]
            album = rel.get("title", "") or ""
            rel_ac = rel.get("artist-credit", []) or []
            albumartist = rel_ac[0].get("name", "") if rel_ac else ""
        rel_date = rec.get("first-release-date", "") or ""
        score = _score_result(
            norm_artist,
            norm_title,
            artist_name or "",
            rec.get("title", "") or "",
            year_q=year,
            res_year=rel_date[:4] if len(rel_date) >= 4 and rel_date[:4].isdigit() else "",
        )
        out.append({
            "id": rec.get("id", ""),
            "title": rec.get("title", ""),
            "artist": artist_name or "",
            "album": album,
            "albumartist": albumartist,
            "date": rel_date,
            "score": score,
        })
    out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return jsonify({"results": out})

@app.route("/api/mb_search_stream", methods=["GET"])
def api_mb_search_stream():
    """SSE streaming MusicBrainz recording search with retry+throttle."""
    if not basic_auth_ok():
        return require_basic_auth()
    title = (request.args.get("title") or "").strip()
    artist = (request.args.get("artist") or "").strip()
    album = (request.args.get("album") or "").strip()
    year = (request.args.get("year") or "").strip()

    def generate():
        if not title and not artist:
            yield sse_event("apierror", "Provide at least a title or artist.")
            return
        norm_title, norm_artist, _ = normalize_search_query(title, artist)
        parts = []
        if norm_title:
            parts.append(f'recording:"{norm_title}"')
        if norm_artist:
            parts.append(f'artist:"{norm_artist}"')
        if album:
            parts.append(f'release:"{album}"')
        if year and year.isdigit():
            parts.append(f'date:{year}')
        q = " AND ".join(parts)
        yield sse_event("log", f"Querying MusicBrainz for: {q!r}\u2026")
        try:
            r = mb_get("https://musicbrainz.org/ws/2/recording",
                       params={"query": q, "fmt": "json", "limit": 10})
            r.raise_for_status()
            data = r.json()
            out = []
            for rec in data.get("recordings", []):
                ac = rec.get("artist-credit", [])
                artist_name = ac[0].get("name") if ac else ""
                releases = rec.get("releases", []) or []
                rec_album = ""
                albumartist = ""
                release_id = ""
                if releases:
                    rel = releases[0]
                    rec_album = rel.get("title", "") or ""
                    rel_ac = rel.get("artist-credit", []) or []
                    albumartist = rel_ac[0].get("name", "") if rel_ac else ""
                    release_id = rel.get("id", "") or ""
                thumb, cover_image = _cover_art_urls_if_exists(release_id)
                rel_date = rec.get("first-release-date", "") or ""
                score = _score_result(
                    norm_artist,
                    norm_title,
                    artist_name or "",
                    rec.get("title", "") or "",
                    year_q=year,
                    res_year=rel_date[:4] if len(rel_date) >= 4 and rel_date[:4].isdigit() else "",
                )
                out.append({
                    "id": rec.get("id", ""),
                    "title": rec.get("title", ""),
                    "artist": artist_name or "",
                    "album": rec_album,
                    "albumartist": albumartist,
                    "date": rel_date,
                    "score": score,
                    "release_id": release_id,
                    "thumb": thumb,
                    "cover_image": cover_image,
                })
            out.sort(key=lambda x: x.get("score", 0), reverse=True)
            yield sse_event("log", f"Found {len(out)} result(s).")
            yield sse_event("result", json.dumps({"results": out}))
        except Exception as e:
            yield sse_event("apierror", str(e))

    return sse_response(generate())

@app.route("/api/mb_resolve", methods=["GET"])
def api_mb_resolve():
    if not basic_auth_ok():
        return require_basic_auth()
    ref = (request.args.get("ref") or "").strip()
    if not ref:
        return jsonify({"error": "Provide a MusicBrainz URL or MBID."}), 400

    mbid = ref
    entity = "recording"
    m = re.search(r"musicbrainz\.org/(recording|release)/([0-9a-fA-F-]{36})", ref)
    if m:
        entity, mbid = m.group(1), m.group(2)

    r = mb_get(f"https://musicbrainz.org/ws/2/{entity}/{mbid}",
                     params={"fmt": "json", "inc": "artist-credits+releases"})
    r.raise_for_status()
    data = r.json()

    if entity == "recording":
        ac = data.get("artist-credit", [])
        artist = ac[0].get("name") if ac else ""
        date = data.get("first-release-date", "") or ""
        releases = data.get("releases", []) or []
        album = ""
        albumartist = ""
        if releases:
            rel = releases[0]
            album = rel.get("title", "") or ""
            rel_ac = rel.get("artist-credit", []) or []
            albumartist = rel_ac[0].get("name", "") if rel_ac else ""
        return jsonify({"fields": {
            "title": data.get("title", "") or "",
            "artist": artist or "",
            "date": date,
            "year": date[:4] if date[:4].isdigit() else "",
            "album": album,
            "albumartist": albumartist,
        }})
    else:
        ac = data.get("artist-credit", [])
        albumartist = ac[0].get("name") if ac else ""
        date = data.get("date", "") or data.get("first-release-date", "") or ""
        return jsonify({"fields": {
            "album": data.get("title", "") or "",
            "albumartist": albumartist or "",
            "date": date,
            "year": date[:4] if date[:4].isdigit() else ""
        }})

@app.route("/api/mb_recording", methods=["GET"])
def api_mb_recording():
    """Full Picard-field lookup for a recording MBID."""
    if not basic_auth_ok():
        return require_basic_auth()
    rec_id = (request.args.get("id") or "").strip()
    if not rec_id:
        return jsonify({"error": "Provide a recording MBID."}), 400

    r = mb_get(
        f"https://musicbrainz.org/ws/2/recording/{rec_id}",
        params={"fmt": "json", "inc": "releases+artist-credits+release-groups"},
    )
    r.raise_for_status()
    data = r.json()

    title = data.get("title", "") or ""
    ac = data.get("artist-credit", []) or []
    artist = ac[0].get("name", "") if ac else ""
    artist_obj = ac[0].get("artist", {}) if ac else {}
    artist_id = artist_obj.get("id", "") if artist_obj else ""

    date = data.get("first-release-date", "") or ""
    year = date[:4] if date[:4].isdigit() else ""

    releases = data.get("releases", []) or []
    album = ""
    albumartist = ""
    albumartist_id = ""
    release_id = ""
    release_group_id = ""
    release_country = ""
    release_status = ""
    release_type = ""
    barcode = ""

    if releases:
        rel = releases[0]
        album = rel.get("title", "") or ""
        release_id = rel.get("id", "") or ""
        release_country = rel.get("country", "") or ""
        release_status = (rel.get("status") or "").lower()
        barcode = rel.get("barcode", "") or ""
        rg = rel.get("release-group", {}) or {}
        release_group_id = rg.get("id", "") or ""
        release_type = (rg.get("primary-type") or "").lower()
        rel_ac = rel.get("artist-credit", []) or []
        albumartist = rel_ac[0].get("name", "") if rel_ac else ""
        rel_artist_obj = rel_ac[0].get("artist", {}) if rel_ac else {}
        albumartist_id = rel_artist_obj.get("id", "") if rel_artist_obj else ""

    fields = {
        "title": title,
        "artist": artist,
        "album": album,
        "albumartist": albumartist,
        "date": date,
        "year": year,
        "musicbrainz_trackid": rec_id,
        "musicbrainz_albumid": release_id,
        "musicbrainz_releasegroupid": release_group_id,
        "musicbrainz_artistid": artist_id,
        "musicbrainz_albumartistid": albumartist_id,
        "musicbrainz_releasecountry": release_country,
        "musicbrainz_releasestatus": release_status,
        "musicbrainz_releasetype": release_type,
        "musicbrainz_albumtype": release_type,
        "musicbrainz_albumstatus": release_status,
        "musicbrainz_albumartist": albumartist,
        "musicbrainz_artist": artist,
        "musicbrainz_album": album,
        "barcode": barcode,
    }
    return jsonify({"fields": {k: v for k, v in fields.items() if v}})

# ----- Discogs (search + release + tracklist picker) -----
@app.route("/api/discogs_search", methods=["GET"])
def api_discogs_search():
    if not basic_auth_ok():
        return require_basic_auth()
    if not DISCOGS_TOKEN:
        return jsonify({"error": "DISCOGS_TOKEN not set."}), 400

    artist = (request.args.get("artist") or "").strip()
    album = (request.args.get("album") or "").strip()
    q = album or (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Provide album or query."}), 400

    params = {"q": q, "type": "release", "per_page": 10}
    if artist:
        params["artist"] = artist

    r = http_get("https://api.discogs.com/database/search",
                 params=params, timeout=25,
                 headers={"Authorization": f"Discogs token={DISCOGS_TOKEN}"})
    r.raise_for_status()
    data = r.json()
    results = []
    for it in data.get("results", []):
        results.append({
            "title": it.get("title", ""),
            "year": it.get("year", ""),
            "id": it.get("id", ""),
            "thumb": it.get("thumb", ""),
            "catno": it.get("catno", ""),
            "label": (it.get("label", [""])[0] if isinstance(it.get("label"), list) else ""),
        })
    return jsonify({"results": results})

@app.route("/api/discogs_release", methods=["GET"])
def api_discogs_release():
    if not basic_auth_ok():
        return require_basic_auth()
    if not DISCOGS_TOKEN:
        return jsonify({"error": "DISCOGS_TOKEN not set."}), 400

    rid = (request.args.get("id") or "").strip()
    if not rid.isdigit():
        return jsonify({"error": "Provide a Discogs release id."}), 400

    r = http_get(f"https://api.discogs.com/releases/{rid}",
                 timeout=25, headers={"Authorization": f"Discogs token={DISCOGS_TOKEN}"})
    r.raise_for_status()
    data = r.json()

    artists = data.get("artists", [])
    albumartist = artists[0].get("name", "") if artists else ""
    title = data.get("title", "") or ""
    year = str(data.get("year", "") or "")

    labels = data.get("labels", []) or []
    label = labels[0].get("name", "") if labels else ""
    catno = labels[0].get("catno", "") if labels else ""

    images = data.get("images", []) or []
    art_url = ""
    if images:
        prim = [i for i in images if i.get("type") == "primary"]
        art_url = (prim[0].get("uri") if prim else images[0].get("uri")) or ""

    # Tracklist for picker
    tracklist = []
    for t in (data.get("tracklist", []) or []):
        tracklist.append({
            "position": t.get("position",""),
            "title": t.get("title",""),
            "duration": t.get("duration",""),
        })

    return jsonify({"fields": {
        "album": title,
        "albumartist": albumartist,
        "year": year if year.isdigit() else "",
        "label": label,
        "catalog_number": catno,
        "art_url": art_url
    }, "tracklist": tracklist})

# ----- URL parsers: Bandcamp / Juno / Traxsource / Beatport -----
def parse_jsonld(html: str):
    scripts = re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S | re.I)
    for s in scripts:
        s = s.strip()
        if not s:
            continue
        try:
            return json.loads(s)
        except Exception:
            continue
    return None

@app.route("/api/parse_url", methods=["GET"])
def api_parse_url():
    """Best-effort parsing for Beatport, Bandcamp, Juno, Traxsource URLs."""
    if not basic_auth_ok():
        return require_basic_auth()

    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Provide a URL."}), 400

    parsed_u = urlparse(url)
    qs = parse_qs(parsed_u.query)
    if "/search" in parsed_u.path or any(k in qs for k in ("q", "term", "keywords")):
        return jsonify({"error": "This looks like a search page URL, not a direct track/product link. Open it manually instead."}), 400

    host = parsed_u.netloc.lower()
    r = http_get(url, timeout=25)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    fields = {}

    # 1) JSON-LD if present (common on many sites)
    j = parse_jsonld(html)
    if isinstance(j, dict):
        # Generic JSON-LD mapping
        fields["title"] = j.get("name", "") or fields.get("title", "")
        # artist
        ba = j.get("byArtist")
        if isinstance(ba, dict):
            fields["artist"] = ba.get("name","") or fields.get("artist","")
        elif isinstance(ba, list) and ba and isinstance(ba[0], dict):
            fields["artist"] = ba[0].get("name","") or fields.get("artist","")
        # date
        dp = j.get("datePublished") or j.get("releaseDate") or ""
        if dp:
            fields["date"] = dp
            if dp[:4].isdigit():
                fields["year"] = dp[:4]
        # image
        img = j.get("image")
        if isinstance(img, str):
            fields["art_url"] = img

    # 2) Site-specific tweaks

    if "bandcamp.com" in host:
        # Bandcamp: look for og tags
        og_title = soup.find("meta", property="og:title")
        og_img = soup.find("meta", property="og:image")
        if og_title and og_title.get("content"):
            # often "Track, by Artist" or "Album, by Artist"
            t = og_title["content"]
            fields["title"] = fields.get("title") or t.split(", by ")[0]
            if ", by " in t:
                fields["artist"] = fields.get("artist") or t.split(", by ")[1]
        if og_img and og_img.get("content"):
            fields["art_url"] = fields.get("art_url") or og_img["content"]

    if "juno.co.uk" in host or "junodownload.com" in host:
        # Juno pages: og:title and product headings
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            fields["title"] = fields.get("title") or og_title["content"]
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            fields["art_url"] = fields.get("art_url") or og_img["content"]

    if "traxsource.com" in host:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            fields["title"] = fields.get("title") or og_title["content"]
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            fields["art_url"] = fields.get("art_url") or og_img["content"]

    if "beatport.com" in host:
        # Beatport: JSON-LD usually works; fallback og tags
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            fields["title"] = fields.get("title") or og_title["content"]
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            fields["art_url"] = fields.get("art_url") or og_img["content"]

    # Clean up
    fields = {k: v for k, v in fields.items() if isinstance(v, str) and v.strip()}
    return jsonify({"fields": fields, "note": "Best-effort parse; verify before writing."})

def _extract_beatport_data_array(html: str) -> list:
    """Locate and extract the JSON array associated with the top-level ``"data"`` key
    from raw Beatport HTML using bracket matching.  Returns the parsed list on success
    or an empty list on any failure.

    The function scans for the literal ``"data":[`` marker and then walks forward,
    counting ``[`` / ``]`` to find the matching closing bracket.  This avoids
    brittle full-JSON parsing while being resilient to large surrounding payloads.
    """
    marker = '"data":['
    idx = html.find(marker)
    if idx == -1:
        return []
    # Advance to the opening '[' of the array
    start = idx + len(marker) - 1  # points at '['
    depth = 0
    end = start
    for i in range(start, len(html)):
        ch = html[i]
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == start:
        return []
    raw = html[start:end + 1]
    try:
        arr = json.loads(raw)
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


# Alternative selectors probed when the primary Bandcamp selector matches nothing.
_BANDCAMP_ALT_SELECTORS = [
    ("li[class*=result]",   lambda soup: soup.find_all("li", class_=re.compile(r"result"))),
    ("div.result-info",     lambda soup: soup.find_all("div", class_="result-info")),
    ("div.heading",         lambda soup: soup.find_all("div", class_="heading")),
    ("a[href*=bandcamp.com/track/]", lambda soup: soup.find_all("a", href=re.compile(r"bandcamp\.com/track/"))),
    (".item-list li",       lambda soup: soup.find_all("li")),
]

def _parse_web_search_results(site: str, search_url: str, html: str,
                              artist_q: str, title_q: str, year_q: str = "",
                              date_q: str = "", remix_tokens: list = None,
                              _debug_info: list = None) -> list:
    """Best-effort extraction of track results from a search results page.

    When *_debug_info* is a list it is populated with diagnostic strings
    describing what the parser found (or failed to find).  This is used by
    the caller to emit SSE ``log`` events without coupling the parser to the
    SSE layer.
    """
    results = []
    soup = BeautifulSoup(html, "html.parser")

    if site == "Bandcamp":
        # HTML scraping of Bandcamp's public search page
        primary_items = soup.find_all("li", class_=re.compile(r"searchresult"))
        if _debug_info is not None:
            _debug_info.append(f"Bandcamp parser: li.searchresult count={len(primary_items)}")
            if not primary_items:
                # Probe alternative selectors to help diagnose markup changes
                alt_counts = []
                for label, selector_fn in _BANDCAMP_ALT_SELECTORS:
                    try:
                        n = len(selector_fn(soup))
                    except Exception:
                        n = 0
                    alt_counts.append(f"{label}:{n}")
                _debug_info.append("Bandcamp parser alt selectors — " + ", ".join(alt_counts))
            elif WEB_SEARCH_DEBUG:
                # Capture a snippet of the first matched node when debug is on
                first_html = str(primary_items[0])[:300]
                _debug_info.append(f"Bandcamp parser first node snippet: {first_html}")
        for item in primary_items:
            heading = item.find(class_="heading")
            subhead = item.find(class_="subhead")

            # Prefer canonical URL from .itemurl; fall back to heading link
            itemurl_el = item.find(class_="itemurl")
            canonical_link = itemurl_el.find("a") if itemurl_el else None
            item_url = canonical_link.get("href") if canonical_link else None
            if not item_url:
                heading_link = heading.find("a") if heading else None
                item_url = heading_link.get("href") if heading_link else None

            t = heading.get_text(strip=True) if heading else ""

            # Artist always from subhead "by ..." (use separator to handle inline elements)
            artist = ""
            if subhead:
                sub = subhead.get_text(separator=" ", strip=True).strip()
                artist = sub[3:].strip() if sub.startswith("by ") else sub

            # Thumbnail: support lazy-load attrs in addition to src
            thumb = ""
            art_el = item.find(class_="art")
            img_el = art_el.find("img") if art_el else item.find("img")
            if img_el:
                for attr in ("src", "data-src", "data-original"):
                    val = img_el.get(attr, "")
                    if val and not val.startswith("data:"):
                        thumb = val
                        break
                if not thumb:
                    srcset = img_el.get("srcset", "")
                    if srcset:
                        thumb = srcset.split(",")[0].strip().split()[0]
            thumb = _bandcamp_thumb_to_full(thumb)

            # Optional: extract release date from .released
            released = ""
            released_el = item.find(class_="released")
            if released_el:
                released_raw = released_el.get_text(strip=True)
                m = re.match(r"released\s+(.+)", released_raw, re.IGNORECASE)
                if m:
                    try:
                        released = dt_datetime.strptime(m.group(1).strip(), "%B %d, %Y").strftime("%Y-%m-%d")
                    except ValueError:
                        released = m.group(1).strip()

            if t and item_url:
                score = _score_result(artist_q, title_q, artist, t, year_q, remix_tokens=remix_tokens, date_q=date_q) + _BANDCAMP_SCORE_BOOST
                entry = {
                    "source": "Bandcamp", "title": t, "artist": artist,
                    "url": item_url,
                    "score": round(min(100.0, score), 1),
                    "direct_url": True, "is_fallback": False,
                }
                if thumb:
                    entry["thumb"] = thumb
                if released:
                    entry["released"] = released
                results.append(entry)

    elif site == "Juno":
        # Primary: productlist_widget_container structure (inspired by mattmurray/juno_crawler)
        for container in soup.find_all("div", class_=re.compile(r"productlist_widget_container")):
            title_el = container.find("div", class_=re.compile(r"productlist_widget_product_title"))
            link_el = title_el.find("a", href=True) if title_el else None
            artist_el = container.find("div", class_=re.compile(r"productlist_widget_product_artists"))
            label_el = container.find("div", class_=re.compile(r"productlist_widget_product_label"))
            img_el = container.find("img", src=True)
            if link_el:
                t = link_el.get_text(strip=True)
                raw_href = link_el["href"]
            elif title_el:
                t = title_el.get_text(strip=True)
                raw_href = ""
            else:
                t = ""
                raw_href = ""
            a = artist_el.get_text(strip=True) if artist_el else ""
            thumb = _juno_thumb_to_full(img_el["src"]) if img_el else ""
            if raw_href and not raw_href.startswith("http"):
                raw_href = "https://www.junodownload.com" + raw_href
            if t and raw_href:
                score = _score_result(artist_q, title_q, a, t, year_q, remix_tokens=remix_tokens, date_q=date_q) + \
                        _compilation_penalty(a)
                entry = {
                    "source": "Juno", "title": t, "artist": a,
                    "url": raw_href,
                    "score": round(max(0.0, score), 1),
                    "direct_url": True, "is_fallback": False,
                }
                if thumb:
                    entry["thumb"] = thumb
                results.append(entry)
        # Secondary: listing items with explicit artist/title elements and rich metadata.
        # Run this regardless of primary matches so we can capture fields like
        # label/genre/BPM/track_number when available.
        for item in soup.find_all("div", class_=re.compile(r"jd-listing-item|track_search_results")):
                title_link_el = item.find("a", class_=re.compile(r"juno-title"))
                artist_wrap_el = item.find(class_=re.compile(r"juno-artist"))
                artist_link_el = artist_wrap_el.find("a") if artist_wrap_el else None
                link_el = title_link_el or item.find("a", href=True)
                img_el = item.find("img", src=True)
                t = title_link_el.get_text(strip=True) if title_link_el else ""
                a = artist_link_el.get_text(strip=True) if artist_link_el else (artist_wrap_el.get_text(strip=True) if artist_wrap_el else "")
                raw_href = link_el["href"] if link_el else ""
                thumb = _juno_thumb_to_full(img_el["src"]) if img_el else ""
                if raw_href and not raw_href.startswith("http"):
                    raw_href = "https://www.junodownload.com" + raw_href
                label = ""
                label_el = item.find("a", class_=re.compile(r"juno-label|label"))
                if label_el:
                    label = label_el.get_text(strip=True)
                genre = ""
                meta_el = item.find(class_=re.compile(r"lit-label-genre|genre"))
                if meta_el:
                    meta_text = meta_el.get_text(" ", strip=True)
                    if label and meta_text.startswith(label):
                        meta_text = meta_text[len(label):].strip(" -|/")
                    genre_parts = [p.strip() for p in re.split(r"\s*\|\s*|\s*\n+\s*", meta_text) if p.strip()]
                    if not label and genre_parts:
                        maybe_label = genre_parts[0]
                        if maybe_label and not re.search(r"/", maybe_label):
                            label = maybe_label
                    if genre_parts:
                        maybe_genre = genre_parts[-1]
                        if maybe_genre != label:
                            genre = maybe_genre
                bpm = None
                bpm_el = item.find(class_=re.compile(r"lit-date-length-tempo|tempo"))
                bpm_text = bpm_el.get_text(" ", strip=True) if bpm_el else ""
                m_bpm = re.search(r"(\d{2,3})\s*BPM", bpm_text, re.IGNORECASE)
                if m_bpm:
                    bpm = m_bpm.group(1)
                # Extract track_number from a.juno-title href query string
                track_number = None
                if title_link_el:
                    tn_qs = parse_qs(urlparse(title_link_el["href"]).query)
                    if "track_number" in tn_qs:
                        track_number = tn_qs["track_number"][0]
                # Fallback: extract from onclick attribute
                if track_number is None and title_link_el:
                    onclick = title_link_el.get("onclick", "") or ""
                    m_tn = re.search(r"track_number\s*:\s*'(\d+)'", onclick)
                    if m_tn:
                        track_number = m_tn.group(1)
                # Additional fallback: cart/button onclick often contains
                # addToCart(title_id, product_id, track_number)
                if track_number is None:
                    atc_btn = item.find("button", class_=re.compile(r"btn-widget-atc"))
                    if atc_btn:
                        onclick = atc_btn.get("onclick", "") or ""
                        m_atc = re.search(r"addToCart\(\s*\d+\s*,\s*\d+\s*,\s*(\d+)\s*\)", onclick)
                        if m_atc:
                            track_number = m_atc.group(1)
                if t and raw_href:
                    score = _score_result(artist_q, title_q, a, t, year_q, remix_tokens=remix_tokens, date_q=date_q) + \
                            _compilation_penalty(a)
                    entry = {
                        "source": "Juno", "title": t, "artist": a,
                        "url": raw_href,
                        "score": round(max(0.0, score), 1),
                        "direct_url": True, "is_fallback": False,
                    }
                    if thumb:
                        entry["thumb"] = thumb
                    if track_number is not None:
                        entry["track_number"] = track_number
                    if label:
                        entry["label"] = label
                    if genre:
                        entry["genre"] = genre
                    if bpm is not None:
                        entry["bpm"] = bpm
                    results.append(entry)
        # fallback: product heading
        if not results:
            for item in soup.find_all("div", class_=re.compile(r"product|juno-track")):
                link_el = item.find("a", href=re.compile(r"/products/"))
                title_el = item.find("span", class_=re.compile(r"title"))
                artist_el = item.find("span", class_=re.compile(r"artist"))
                img_el = item.find("img", src=True)
                t = title_el.get_text(strip=True) if title_el else ""
                a = artist_el.get_text(strip=True) if artist_el else ""
                thumb = _juno_thumb_to_full(img_el["src"]) if img_el else ""
                if t and link_el:
                    url = "https://www.junodownload.com" + link_el["href"]
                    score = _score_result(artist_q, title_q, a, t, year_q, remix_tokens=remix_tokens, date_q=date_q) + \
                            _compilation_penalty(a)
                    entry = {
                        "source": "Juno", "title": t, "artist": a,
                        "url": url, "score": round(max(0.0, score), 1),
                        "direct_url": True, "is_fallback": False,
                    }
                    if thumb:
                        entry["thumb"] = thumb
                    results.append(entry)

    elif site == "Traxsource":
        for item in soup.find_all("div", class_=re.compile(r"\btrk-row\b")):
            if "play-trk" not in (item.get("class") or []):
                continue
            # Thumbnail
            thumb_cell = item.find("div", class_="thumb")
            img_el = thumb_cell.find("img", src=True) if thumb_cell else None
            thumb = img_el["src"] if img_el else ""
            # Track URL and title
            title_cell = item.find("div", class_="title")
            link_el = title_cell.find("a", href=True) if title_cell else None
            if not link_el:
                continue
            raw_href = link_el["href"]
            url = raw_href if raw_href.startswith("http") else "https://www.traxsource.com" + raw_href
            # Version/remix from span.version (strip trailing duration like "(4:38)")
            version_el = title_cell.find("span", class_="version") if title_cell else None
            version_text = ""
            if version_el:
                version_text = re.sub(r"\s*\(\d+:\d+\)\s*$", "", version_el.get_text(strip=True)).strip()
            # Base title: link text minus any nested version span text
            raw_link_text = link_el.get_text(strip=True)
            if version_el:
                ver_raw = version_el.get_text(strip=True)
                if raw_link_text.endswith(ver_raw):
                    raw_link_text = raw_link_text[:-len(ver_raw)].strip()
            t = f"{raw_link_text} ({version_text})" if version_text else raw_link_text
            if not t:
                continue
            # Artists
            artists_cell = item.find("div", class_="artists")
            a = ", ".join(
                a_el.get_text(strip=True)
                for a_el in (artists_cell.find_all("a") if artists_cell else [])
            )
            # Label
            label_cell = item.find("div", class_="label")
            label_el = label_cell.find("a") if label_cell else None
            label = label_el.get_text(strip=True) if label_el else ""
            # Genre (take first segment if contains " / ")
            genre_cell = item.find("div", class_="genre")
            genre_el = genre_cell.find("a") if genre_cell else None
            genre_raw = genre_el.get_text(strip=True) if genre_el else ""
            genre = genre_raw.split(" / ")[0] if " / " in genre_raw else genre_raw
            # Release date
            rdate_cell = item.find("div", class_="r-date")
            released = rdate_cell.get_text(strip=True) if rdate_cell else ""
            # Score
            score = _score_result(artist_q, title_q, a, t, year_q, remix_tokens=remix_tokens, date_q=date_q) + \
                    _compilation_penalty(a)
            entry = {
                "source": "Traxsource", "title": t, "artist": a,
                "url": url, "score": round(max(0.0, score), 1),
                "direct_url": True, "is_fallback": False,
            }
            if thumb:
                entry["thumb"] = thumb
            if released:
                entry["released"] = released
            if label:
                entry["label"] = label
            if genre:
                entry["genre"] = genre
            results.append(entry)

    elif site == "Beatport":
        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data and next_data.string:
            try:
                nd = json.loads(next_data.string)
                queries = (nd.get("props", {}).get("pageProps", {})
                           .get("dehydratedState", {}).get("queries", []))
                for q_item in queries:
                    tracks_data = (q_item.get("state", {}).get("data", {})
                                   .get("tracks", {}).get("data", []) or [])
                    for track in tracks_data:
                        t = track.get("name", "") or ""
                        artists = track.get("artists", []) or []
                        a = ", ".join(x.get("name", "") for x in artists if x.get("name"))
                        slug = track.get("slug", "")
                        tid = track.get("id", "")
                        url = f"https://www.beatport.com/track/{slug}/{tid}" if slug else search_url
                        # Extract year from release data
                        release = track.get("release") or {}
                        release_date = (release.get("new_release_date") or
                                        track.get("new_release_date") or "")
                        res_year = release_date[:4] if release_date and release_date[:4].isdigit() else ""
                        # Compilation detection
                        release_type = (release.get("type") or "").lower()
                        track_count = int(release.get("track_count") or 0)
                        # Extra fields
                        bpm_val = track.get("bpm") or ""
                        genres = track.get("genres") or track.get("genre") or []
                        if isinstance(genres, list):
                            genre_str = ", ".join(g.get("name", "") for g in genres if g.get("name"))
                        elif isinstance(genres, dict):
                            genre_str = genres.get("name", "")
                        else:
                            genre_str = ""
                        key_data = track.get("key") or {}
                        if isinstance(key_data, dict):
                            key_str = key_data.get("name", "") or key_data.get("camelot", "")
                        else:
                            key_str = str(key_data) if key_data else ""
                        label_data = release.get("label") or track.get("label") or {}
                        if isinstance(label_data, dict):
                            label_str = label_data.get("name", "")
                        else:
                            label_str = str(label_data) if label_data else ""
                        remixers = track.get("remixers") or []
                        remixer_str = ", ".join(x.get("name", "") for x in remixers if x.get("name"))
                        # Thumbnail: prefer track image, fall back to release image or URI fields
                        images = track.get("images") or release.get("images") or {}
                        if isinstance(images, dict):
                            thumb = (
                                (images.get("small") or {}).get("uri")
                                or (images.get("medium") or {}).get("uri")
                                or images.get("uri")
                                or ""
                            )
                        else:
                            thumb = track.get("image") or release.get("image") or ""
                        if not thumb:
                            thumb = (
                                track.get("release_image_uri")
                                or release.get("release_image_uri")
                                or track.get("image_uri")
                                or ""
                            )
                        if t and slug:
                            _prox_bonus, _prox_unlocks = _beatport_date_proximity_score(date_q, release_date)
                            score = _score_result(artist_q, title_q, a, t, year_q, res_year, remix_tokens, date_q=date_q) + \
                                    _compilation_penalty(a, release_type, track_count) + \
                                    _prox_bonus
                            # Perfect 100.0 only when exact date match; otherwise cap at 99.9
                            if not _prox_unlocks:
                                score = min(99.9, score)
                            entry = {
                                "source": "Beatport", "title": t, "artist": a,
                                "url": url, "score": round(max(0.0, score), 2),
                                "genre": genre_str,
                                "bpm": str(bpm_val) if bpm_val else "",
                                "key": key_str,
                                "released": release_date,
                                "label": label_str,
                                "remixers": remixer_str,
                                "direct_url": True, "is_fallback": False,
                            }
                            if thumb:
                                entry["thumb"] = thumb
                            results.append(entry)
            except Exception:
                pass
        # Beatport secondary fallback: scan raw HTML for "data":[{...}] array
        if not results:
            try:
                data_arr = _extract_beatport_data_array(html)
                for track in data_arr[:10]:
                    if not isinstance(track, dict):
                        continue
                    t = track.get("track_name", "") or ""
                    if not t:
                        continue
                    artists = track.get("artists", []) or []
                    if isinstance(artists, list):
                        a = ", ".join(
                            x.get("artist_name", "") for x in artists
                            if isinstance(x, dict) and x.get("artist_name")
                        )
                    else:
                        a = ""
                    # URL: try to build a direct track URL from track_id / slug
                    tid = track.get("track_id") or track.get("id") or ""
                    slug = track.get("slug", "") or ""
                    if tid and slug:
                        url = f"https://www.beatport.com/track/{slug}/{tid}"
                        direct_url = True
                    elif tid:
                        url = f"https://www.beatport.com/track/-/{tid}"
                        direct_url = True
                    else:
                        url = search_url
                        direct_url = False
                    # Release date / year
                    release_date = (
                        track.get("release_date") or
                        (track.get("release") or {}).get("release_date") or ""
                    )
                    res_year = release_date[:4] if release_date and release_date[:4].isdigit() else ""
                    # BPM
                    bpm_val = track.get("bpm") or ""
                    # Key
                    key_str = track.get("key_name") or ""
                    if not key_str:
                        key_data = track.get("key") or {}
                        if isinstance(key_data, dict):
                            key_str = key_data.get("name", "") or key_data.get("camelot", "")
                        else:
                            key_str = str(key_data) if key_data else ""
                    # Genre
                    genres = track.get("genre") or track.get("genres") or []
                    if isinstance(genres, list):
                        genre_str = ", ".join(
                            g.get("genre_name", "") or g.get("name", "")
                            for g in genres
                            if isinstance(g, dict)
                        )
                    elif isinstance(genres, dict):
                        genre_str = genres.get("genre_name", "") or genres.get("name", "")
                    else:
                        genre_str = ""
                    # Label
                    label_data = track.get("label") or {}
                    if isinstance(label_data, dict):
                        label_str = label_data.get("label_name", "") or label_data.get("name", "")
                    else:
                        label_str = str(label_data) if label_data else ""
                    # Thumbnail
                    images = track.get("images") or (track.get("release") or {}).get("images") or {}
                    if isinstance(images, dict):
                        thumb = (
                            (images.get("small") or {}).get("uri")
                            or (images.get("medium") or {}).get("uri")
                            or images.get("uri")
                            or ""
                        )
                    else:
                        thumb = track.get("image") or ""
                    if not thumb:
                        thumb = (
                            track.get("release_image_uri")
                            or (track.get("release") or {}).get("release_image_uri")
                            or track.get("image_uri")
                            or ""
                        )
                    _prox_bonus, _prox_unlocks = _beatport_date_proximity_score(date_q, release_date)
                    score = (
                        _score_result(artist_q, title_q, a, t, year_q, res_year, remix_tokens, date_q=date_q)
                        + _compilation_penalty(a)
                        + _prox_bonus
                    )
                    # Perfect 100.0 only when exact date match; otherwise cap at 99.9
                    if not _prox_unlocks:
                        score = min(99.9, score)
                    entry = {
                        "source": "Beatport", "title": t, "artist": a,
                        "url": url, "score": round(max(0.0, score), 2),
                        "genre": genre_str,
                        "bpm": str(bpm_val) if bpm_val else "",
                        "key": key_str,
                        "released": release_date,
                        "label": label_str,
                        "direct_url": direct_url, "is_fallback": False,
                    }
                    if thumb:
                        entry["thumb"] = thumb
                    results.append(entry)
            except Exception:
                pass

        # Beatport final fallback: try og:title/og:description if still nothing
        if not results:
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                t = og_title["content"]
                results.append({
                    "source": "Beatport",
                    "title": t,
                    "artist": "",
                    "url": search_url,
                    # No release_date available here; date proximity is (0, False),
                    # so cap at 99 is consistent with the proximity-required-for-100 rule.
                    "score": min(99.0, _score_result(artist_q, title_q, "", t, year_q, remix_tokens=remix_tokens, date_q=date_q)),
                    "note": "Parsed from og:title; click to search manually.",
                    "direct_url": False, "is_fallback": True,
                })

    # If nothing structured was found, return the search URL as a fallback entry
    if not results:
        results.append({
            "source": site,
            "title": f"View search results on {site}",
            "artist": "",
            "url": search_url,
            "score": 0.0,
            "note": "No structured results extracted; open manually.",
            "direct_url": False, "is_fallback": True,
        })
    return results


@app.route("/api/web_search_stream", methods=["GET"])
def api_web_search_stream():
    if not basic_auth_ok():
        return require_basic_auth()
    artist = (request.args.get("artist") or "").strip()
    title = (request.args.get("title") or "").strip()
    year = (request.args.get("year") or "").strip()
    date = (request.args.get("date") or "").strip()
    q_raw = (request.args.get("q") or "").strip()

    # If q was explicitly provided, it is the source of truth for the outgoing
    # search query URL.  For scoring, prefer explicit artist/title form fields
    # when available (they are typically cleaner than a free-form q string).
    if q_raw:
        q = q_raw
        if artist or title:
            norm_title, norm_artist, remix_tokens = normalize_search_query(title, artist)
        else:
            q_artist, q_title = _split_query_artist_title(q_raw)
            norm_title, norm_artist, remix_tokens = normalize_search_query(
                q_title or q_raw, q_artist
            )
    else:
        q = f"{artist} {title}".strip()
        norm_title, norm_artist, remix_tokens = normalize_search_query(title, artist)
    norm_q = f"{norm_artist} {norm_title}".strip() if (norm_artist or norm_title) else q
    # Append remix identity words so sources can find remix-specific results
    if remix_tokens:
        _identity_words = []
        for _tok in remix_tokens:
            _norm = _normalize_remix_handle(_tok).lower()
            _identity_words.extend(
                w for w in re.findall(r'\w+', _norm)
                if not _REMIX_KW_RE.match(w) and len(w) > 2
            )
        if _identity_words:
            norm_q = f"{norm_q} {' '.join(dict.fromkeys(_identity_words))}".strip()

    norm_q_out = norm_q.lower()
    retry_q_out = _build_retry_query(norm_artist, norm_title)
    # Retry is meaningful when the original query included a remix descriptor
    # (bracketed token or bare trailing keyword), so searching without it
    # casts a broader net and may surface compilation/release pages.
    _retry_meaningful = bool(remix_tokens) or (
        _TRAILING_REMIX_RE.sub("", norm_title).strip(" -,") != norm_title
    )

    def generate():
        if not q:
            yield sse_event("apierror", "Provide a query.")
            return
        qq = requests.utils.quote(norm_q_out, safe="")
        yield sse_event("log", f"Query: q={q!r} \u2192 norm_q={norm_q!r} \u2192 norm_q_out={norm_q_out!r} \u2192 qq={qq!r}")
        html_sites = [
            ("Beatport",    f"https://www.beatport.com/search/tracks?q={qq}"),
            ("Traxsource",  f"https://www.traxsource.com/search?term={qq}&page=1&type=tracks"),
            ("Juno",        f"https://www.junodownload.com/search/?solrorder=relevancy&q%5Btitle%5D%5B0%5D={qq}"),
        ]
        bandcamp_search_url = f"https://bandcamp.com/search?q={qq}&item_type=t"
        results_by_source = {}
        # Lazy-initialized shared Playwright browser for headless fallbacks within this request.
        # Using a list so the nested closure can rebind the reference.
        _hl_pw = [None]
        _hl_browser = [None]
        def _ensure_hl_browser():
            if not _PLAYWRIGHT_AVAILABLE:
                raise RuntimeError(
                    "Playwright not installed; install playwright and run 'playwright install chromium'"
                )
            if _hl_browser[0] is None:
                _hl_pw[0] = _sync_playwright().start()
                _hl_browser[0] = _hl_pw[0].chromium.launch(headless=True)
            return _hl_browser[0]
        try:
            for site_name, search_url in html_sites:
                yield sse_event("log", f"Searching {site_name}\u2026")
                _site_headless_reason = ""
                try:
                    r = http_get(search_url, timeout=15)
                    found = _parse_web_search_results(
                        site_name, search_url, r.text,
                        norm_artist, norm_title, year, date, remix_tokens,
                    )
                    if site_name in ("Juno", "Traxsource"):
                        if r.status_code in (403, 429) or r.status_code >= 500:
                            _site_headless_reason = f"HTTP {r.status_code}"
                        elif all(x.get("is_fallback") for x in found):
                            _site_headless_reason = "no structured results"
                except Exception as exc:
                    yield sse_event("log", f"{site_name}: error \u2014 {str(exc)[:120]}")
                    found = [{"source": site_name, "title": f"View search results on {site_name}",
                              "artist": "", "url": search_url, "score": 0.0,
                              "note": "Search failed; open manually.",
                              "direct_url": False, "is_fallback": True}]
                    if site_name in ("Juno", "Traxsource"):
                        _site_headless_reason = f"error \u2014 {str(exc)[:60]}"
                # Headless fallback for Juno / Traxsource
                if HEADLESS_ENABLED and _site_headless_reason:
                    yield sse_event("log", f"{site_name}: scraping failed ({_site_headless_reason}) \u2014 switching to headless")
                    try:
                        _hl_html = _headless_get_html(search_url, HEADLESS_TIMEOUT_SECS,
                                                      _browser=_ensure_hl_browser())
                        if WEB_SEARCH_DEBUG:
                            yield sse_event("log", f"{site_name} headless HTML preview: {_hl_html[:400]!r}")
                        _hl_found = _parse_web_search_results(
                            site_name, search_url, _hl_html,
                            norm_artist, norm_title, year, date, remix_tokens,
                        )
                        _hl_non_fb = [x for x in _hl_found if not x.get("is_fallback")]
                        if _hl_non_fb:
                            found = _hl_found[:HEADLESS_MAX_RESULTS]
                            yield sse_event("log", f"{site_name} headless: {len(_hl_non_fb)} result(s)")
                        else:
                            yield sse_event("log", f"{site_name} headless: no structured results found")
                    except Exception as _hl_exc:
                        yield sse_event("log", f"{site_name} headless: failed \u2014 {str(_hl_exc)[:120]}")
                # Per-site remix-retry: trigger when first pass has no good matches
                # (no structured results, or best score below threshold).
                _non_fb_first = [x for x in found if not x.get("is_fallback")]
                _first_count = len(_non_fb_first)
                _first_best = max((x.get("score", 0) for x in _non_fb_first), default=0)
                if _retry_meaningful:
                    yield sse_event("log", f"{site_name}: pass 1 q={norm_q_out!r} \u2014 {_first_count} result(s), best score {_first_best}")
                if _should_retry_without_remix(_retry_meaningful, retry_q_out, norm_q_out, _first_best, _first_count):
                    _retry_qq = requests.utils.quote(retry_q_out, safe="")
                    _retry_url = _site_search_url(site_name, _retry_qq)
                    if _retry_url:
                        yield sse_event("log", f"{site_name}: retrying without remix \u2014 {norm_q_out!r} \u2192 {retry_q_out!r}")
                        try:
                            _r_retry = http_get(_retry_url, timeout=15)
                            _retry_found = _parse_web_search_results(
                                site_name, _retry_url, _r_retry.text,
                                norm_artist, norm_title, year, date, remix_tokens,
                            )
                            _retry_non_fb = [x for x in _retry_found if not x.get("is_fallback")]
                            _retry_best = max((x.get("score", 0) for x in _retry_non_fb), default=0)
                            yield sse_event("log", f"{site_name}: pass 2 q={retry_q_out!r} \u2014 {len(_retry_non_fb)} result(s), best score {_retry_best}")
                            if _retry_non_fb:
                                _merged_non_fb = _deduplicate_by_url(_non_fb_first + _retry_non_fb)
                                _fb_items = [x for x in found if x.get("is_fallback")]
                                found = _merged_non_fb + _fb_items
                                yield sse_event("log", f"{site_name}: retry added {len(_retry_non_fb)} result(s); merged total {len(_merged_non_fb)} structured result(s)")
                            else:
                                yield sse_event("log", f"{site_name}: retry found no structured results \u2014 keeping first-pass")
                        except Exception as _retry_exc:
                            yield sse_event("log", f"{site_name}: retry failed \u2014 {str(_retry_exc)[:80]}")
                yield sse_event("log", f"{site_name}: {len(found)} result(s)")
                non_fb = [x for x in found if not x.get("is_fallback")]
                # De-duplicate within source by URL
                deduped = _deduplicate_by_url(non_fb)
                if site_name == "Juno" and deduped and max((x.get("score", 0) for x in deduped), default=0) <= 0:
                    deduped = []
                    yield sse_event("log", "Juno: structured matches scored 0; returning search-page link instead")
                found = _drop_zero_score_structured(found, site_name, search_url)
                non_fb = [x for x in found if not x.get("is_fallback")]
                deduped = _deduplicate_by_url(non_fb)
                results_by_source[site_name] = (
                    sorted(deduped, key=lambda x: x.get("score", 0), reverse=True)[:5]
                    if deduped else found[:1]
                )
            # Bandcamp: scrape the public search page HTML
            yield sse_event("log", "Searching Bandcamp\u2026")
            _bc_headless_reason = ""  # non-empty signals that headless should be attempted
            try:
                r = bandcamp_get(bandcamp_search_url, timeout=15)
                bc_html = r.text
                bc_lower = bc_html.lower()
                # HTTP-level diagnostics (always emitted for actionability)
                final_url = r.url
                status_code = r.status_code
                content_type = r.headers.get("content-type", "")
                html_len = len(bc_html)
                markers = {
                    "searchresult":            "searchresult" in bc_lower,
                    "itemurl":                 "itemurl" in bc_lower,
                    "bandcamp.com/track/":     "bandcamp.com/track/" in bc_lower,
                    "captcha":                 "captcha" in bc_lower,
                    "cloudflare":              "cloudflare" in bc_lower,
                    "enable javascript":       "enable javascript" in bc_lower,
                    "consent":                 "consent" in bc_lower,
                }
                marker_str = " ".join(f"{k}={'Y' if v else 'N'}" for k, v in markers.items())
                yield sse_event("log", (
                    f"Bandcamp HTTP: status={status_code} len={html_len} "
                    f"ct={content_type!r} final_url={final_url}"
                ))
                if status_code >= 400:
                    yield sse_event("log", f"Bandcamp is blocking the request (HTTP {status_code}); results may be unavailable.")
                    _bc_headless_reason = f"HTTP {status_code}"
                yield sse_event("log", f"Bandcamp markers: {marker_str}")
                if WEB_SEARCH_DEBUG:
                    yield sse_event("log", f"Bandcamp HTML preview: {bc_html[:400]!r}")
                bc_debug: list = []
                found = _parse_web_search_results(
                    "Bandcamp", bandcamp_search_url, bc_html,
                    norm_artist, norm_title, year, date, remix_tokens,
                    _debug_info=bc_debug,
                )
                for msg in bc_debug:
                    yield sse_event("log", msg)
                if not _bc_headless_reason and all(x.get("is_fallback") for x in found):
                    _bc_headless_reason = "no structured results"
            except Exception as exc:
                yield sse_event("log", f"Bandcamp: error \u2014 {str(exc)[:120]}")
                found = [{"source": "Bandcamp", "title": "View search results on Bandcamp",
                          "artist": "", "url": bandcamp_search_url, "score": 0.0,
                          "note": "Search failed; open manually.",
                          "direct_url": False, "is_fallback": True}]
                _bc_headless_reason = f"error \u2014 {str(exc)[:60]}"
            # Headless fallback for Bandcamp
            if HEADLESS_ENABLED and _bc_headless_reason:
                yield sse_event("log", f"Bandcamp: scraping failed ({_bc_headless_reason}) \u2014 switching to headless")
                try:
                    _bc_hl_html = _headless_get_html(bandcamp_search_url, HEADLESS_TIMEOUT_SECS,
                                                     _browser=_ensure_hl_browser())
                    if WEB_SEARCH_DEBUG:
                        yield sse_event("log", f"Bandcamp headless HTML preview: {_bc_hl_html[:400]!r}")
                    _bc_hl_debug: list = []
                    _bc_hl_found = _parse_web_search_results(
                        "Bandcamp", bandcamp_search_url, _bc_hl_html,
                        norm_artist, norm_title, year, date, remix_tokens,
                        _debug_info=_bc_hl_debug,
                    )
                    for msg in _bc_hl_debug:
                        yield sse_event("log", f"Bandcamp headless: {msg}")
                    _bc_hl_non_fb = [x for x in _bc_hl_found if not x.get("is_fallback")]
                    if _bc_hl_non_fb:
                        found = _bc_hl_found[:HEADLESS_MAX_RESULTS]
                        yield sse_event("log", f"Bandcamp headless: {len(_bc_hl_non_fb)} result(s)")
                    else:
                        yield sse_event("log", "Bandcamp headless: no structured results found")
                except Exception as _bc_hl_exc:
                    yield sse_event("log", f"Bandcamp headless: failed \u2014 {str(_bc_hl_exc)[:120]}")
            # Per-site remix-retry for Bandcamp: trigger when first pass has no good
            # matches (no structured results, or best score below threshold).
            _bc_non_fb_first = [x for x in found if not x.get("is_fallback")]
            _bc_first_count = len(_bc_non_fb_first)
            _bc_first_best = max((x.get("score", 0) for x in _bc_non_fb_first), default=0)
            if _retry_meaningful:
                yield sse_event("log", f"Bandcamp: pass 1 q={norm_q_out!r} \u2014 {_bc_first_count} result(s), best score {_bc_first_best}")
            if _should_retry_without_remix(_retry_meaningful, retry_q_out, norm_q_out, _bc_first_best, _bc_first_count):
                _bc_retry_qq = requests.utils.quote(retry_q_out, safe="")
                _bc_retry_url = f"https://bandcamp.com/search?q={_bc_retry_qq}&item_type=t"
                yield sse_event("log", f"Bandcamp: retrying without remix \u2014 {norm_q_out!r} \u2192 {retry_q_out!r}")
                try:
                    _bc_r_retry = bandcamp_get(_bc_retry_url, timeout=15)
                    _bc_retry_debug: list = []
                    _bc_retry_found = _parse_web_search_results(
                        "Bandcamp", _bc_retry_url, _bc_r_retry.text,
                        norm_artist, norm_title, year, date, remix_tokens,
                        _debug_info=_bc_retry_debug,
                    )
                    for msg in _bc_retry_debug:
                        yield sse_event("log", f"Bandcamp retry: {msg}")
                    _bc_retry_non_fb = [x for x in _bc_retry_found if not x.get("is_fallback")]
                    _bc_retry_best = max((x.get("score", 0) for x in _bc_retry_non_fb), default=0)
                    yield sse_event("log", f"Bandcamp: pass 2 q={retry_q_out!r} \u2014 {len(_bc_retry_non_fb)} result(s), best score {_bc_retry_best}")
                    if _bc_retry_non_fb:
                        _bc_merged_non_fb = _deduplicate_by_url(_bc_non_fb_first + _bc_retry_non_fb)
                        _bc_fb_items = [x for x in found if x.get("is_fallback")]
                        found = _bc_merged_non_fb + _bc_fb_items
                        yield sse_event("log", f"Bandcamp: retry added {len(_bc_retry_non_fb)} result(s); merged total {len(_bc_merged_non_fb)} structured result(s)")
                    else:
                        yield sse_event("log", f"Bandcamp: retry found no structured results \u2014 keeping first-pass")
                except Exception as _bc_retry_exc:
                    yield sse_event("log", f"Bandcamp: retry failed \u2014 {str(_bc_retry_exc)[:80]}")
            yield sse_event("log", f"Bandcamp: {len(found)} result(s)")
            non_fb = [x for x in found if not x.get("is_fallback")]
            deduped = _deduplicate_by_url(non_fb)
            results_by_source["Bandcamp"] = (
                sorted(deduped, key=lambda x: x.get("score", 0), reverse=True)[:5]
                if deduped else found[:1]
            )
            total = sum(len(v) for v in results_by_source.values())
            yield sse_event("log", f"Total {total} results across {len(results_by_source)} sources")
            yield sse_event("result", json.dumps({"results_by_source": _truncate_results_by_source(results_by_source)}))
        finally:
            # Close shared headless browser if it was opened for this request
            if _hl_browser[0] is not None:
                try:
                    _hl_browser[0].close()
                except Exception:
                    pass
            if _hl_pw[0] is not None:
                try:
                    _hl_pw[0].stop()
                except Exception:
                    pass

    return sse_response(generate())

# ----- SSE streaming helpers -----
_MAX_SSE_LOG = 3000  # max chars for log/error messages (truncated with ellipsis if exceeded)
_MAX_FIELD_LEN = 500  # max chars per string field before JSON encoding to prevent huge payloads

def sse_event(event: str, data: str) -> str:
    # Only truncate non-result events; result events always carry JSON and must not be cut mid-token.
    if event != "result" and len(data) > _MAX_SSE_LOG:
        data = data[:_MAX_SSE_LOG] + "\u2026"
    data_lines = "\n".join(f"data: {line}" for line in data.split("\n"))
    return f"event: {event}\n{data_lines}\n\n"

def _truncate_result_fields(results: list) -> list:
    """Truncate long string fields in result dicts before JSON encoding."""
    truncated = []
    for item in results:
        entry = {}
        for k, v in item.items():
            if isinstance(v, str) and len(v) > _MAX_FIELD_LEN:
                entry[k] = v[:_MAX_FIELD_LEN] + "\u2026"
            else:
                entry[k] = v
        truncated.append(entry)
    return truncated

def _truncate_results_by_source(rbs: dict) -> dict:
    """Truncate long string fields in a results_by_source dict before JSON encoding."""
    return {source: _truncate_result_fields(results) for source, results in rbs.items()}

def _merge_result_entries(base: dict, candidate: dict) -> dict:
    """Merge two same-URL result dicts, preserving richer metadata.

    Preference rules:
      - Keep the higher score.
      - Prefer direct URLs over non-direct/fallback variants.
      - Fill empty fields from the candidate (track_number, bpm, genre, etc.).
    """
    merged = dict(base)
    if candidate.get("score", 0) > merged.get("score", 0):
        merged["score"] = candidate.get("score", 0)
    if candidate.get("direct_url") and not merged.get("direct_url"):
        merged["direct_url"] = True
    if not candidate.get("is_fallback"):
        merged["is_fallback"] = False
    for k, v in candidate.items():
        if k in ("score", "direct_url", "is_fallback"):
            continue
        if v in (None, ""):
            continue
        if merged.get(k) in (None, ""):
            merged[k] = v
    return merged


def _deduplicate_by_url(results: list) -> list:
    """Return results with duplicate URLs merged, preserving first-seen order."""
    by_url: dict = {}
    order = []
    for x in results:
        u = x.get("url", "")
        if not u:
            order.append(x)
            continue
        if u not in by_url:
            by_url[u] = dict(x)
            order.append(by_url[u])
            continue
        by_url[u] = _merge_result_entries(by_url[u], x)
        for idx, item in enumerate(order):
            if isinstance(item, dict) and item.get("url") == u:
                order[idx] = by_url[u]
                break
    return order


def _drop_zero_score_structured(results: list, site_name: str, search_url: str) -> list:
    """Drop structured results with zero score; keep fallback when nothing useful remains."""
    kept = [x for x in results if x.get("is_fallback") or x.get("score", 0) > 0]
    if kept:
        return kept
    return [{
        "source": site_name,
        "title": f"View search results on {site_name}",
        "artist": "",
        "url": search_url,
        "score": 0.0,
        "note": "No non-zero matches extracted; open manually.",
        "direct_url": False,
        "is_fallback": True,
    }]

def sse_response(gen):
    return Response(gen, content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ----- AcoustID -> MB recording resolve -----
import tempfile
import subprocess

def _parse_acoustid_response(raw_data):
    """Yield dicts with score/recording_id/title/artist/album/albumartist/year
    from a raw AcoustID API response (parse=False).  Resilient to missing fields."""
    for res_item in (raw_data.get("results") or []):
        score = float(res_item.get("score", 0.0))
        for rec in (res_item.get("recordings") or []):
            recording_id = rec.get("id") or ""
            title = rec.get("title") or ""
            rec_artists = rec.get("artists") or []
            artist = rec_artists[0].get("name") or "" if rec_artists else ""

            releases = rec.get("releases") or []
            album = ""
            albumartist = ""
            year = ""
            if releases:
                rel = releases[0]
                album = rel.get("title") or ""
                rel_artists = rel.get("artists") or []
                albumartist = rel_artists[0].get("name") or "" if rel_artists else ""
                date_obj = rel.get("date") or {}
                if isinstance(date_obj, dict) and date_obj.get("year"):
                    year = str(date_obj["year"])

            yield {
                "score": score,
                "recording_id": recording_id,
                "title": title,
                "artist": artist,
                "album": album,
                "albumartist": albumartist,
                "year": year,
            }

@app.route("/api/acoustid", methods=["GET"])
def api_acoustid():
    if not basic_auth_ok():
        return require_basic_auth()
    if not ACOUSTID_KEY:
        return jsonify({"error": "ACOUSTID_KEY not set."}), 400

    path = safe_path(request.args.get("path", ""))

    def do_match(pth: str):
        return acoustid.match(ACOUSTID_KEY, pth, meta="recordings releases", parse=False)

    try:
        results = do_match(path)
    except Exception as e:
        msg = str(e).lower()
        # Try ffmpeg->wav fallback for decode issues
        if "could not be decoded" in msg or "decode" in msg:
            try:
                with tempfile.TemporaryDirectory() as td:
                    wav = os.path.join(td, "tmp.wav")
                    # decode first 60 seconds to wav (enough for fingerprint)
                    proc = subprocess.run(
                        ["ffmpeg", "-y", "-v", "error", "-i", path, "-t", "60", "-ac", "2", "-ar", "44100", wav],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                    )
                    if proc.returncode != 0:
                        return jsonify({"error": f"AcoustID decode failed; ffmpeg error: {proc.stderr.strip()[:400]}"}), 400
                    results = do_match(wav)
            except Exception as e2:
                return jsonify({"error": f"AcoustID lookup failed after ffmpeg fallback: {e2}"}), 400
        else:
            return jsonify({"error": f"AcoustID lookup failed: {e}"}), 400

    out = list(islice(_parse_acoustid_response(results), 10))
    return jsonify({"results": out})

# ----- Streaming endpoints (SSE) -----

@app.route("/api/discogs_search_stream", methods=["GET"])
def api_discogs_search_stream():
    if not basic_auth_ok():
        return require_basic_auth()
    artist = (request.args.get("artist") or "").strip()
    album = (request.args.get("album") or "").strip()
    q = album or (request.args.get("q") or "").strip()

    # Normalize album and artist for the search query
    norm_q, norm_artist, _ = normalize_search_query(q, artist)
    if not norm_q:
        norm_q = q
    if not norm_artist:
        norm_artist = artist

    def generate():
        if not DISCOGS_TOKEN:
            yield sse_event("apierror", "DISCOGS_TOKEN not set.")
            return
        if not norm_q:
            yield sse_event("apierror", "Provide album or query.")
            return
        yield sse_event("log", f"Searching Discogs for: {norm_q!r}\u2026")
        try:
            params = {"q": norm_q, "type": "release", "per_page": 10}
            if norm_artist:
                params["artist"] = norm_artist
            yield sse_event("log", f"Auth: token present ({obfuscate_key(DISCOGS_TOKEN)}), scheme='Discogs token=...'")
            r = http_get("https://api.discogs.com/database/search",
                         params=params, timeout=25,
                         headers={"Authorization": f"Discogs token={DISCOGS_TOKEN}"})
            r.raise_for_status()
            data = r.json()
            results = []
            for it in data.get("results", []):
                title_txt = it.get("title", "")
                album_score = _similarity(norm_q, title_txt)
                artist_score = _similarity(norm_artist, title_txt) if norm_artist else 0.0
                combined_score = int(round(((album_score * 0.8) + (artist_score * 0.2)) * 100))
                results.append({
                    "title": it.get("title", ""),
                    "year": it.get("year", ""),
                    "id": it.get("id", ""),
                    "thumb": it.get("thumb", ""),
                    "cover_image": it.get("cover_image", ""),
                    "catno": it.get("catno", ""),
                    "label": (it.get("label", [""])[0] if isinstance(it.get("label"), list) else ""),
                    "uri": it.get("uri", ""),
                    "score": combined_score,
                })
            yield sse_event("log", f"Found {len(results)} result(s).")
            yield sse_event("result", json.dumps({"results": results}))
        except Exception as e:
            yield sse_event("apierror", str(e))

    return sse_response(generate())

@app.route("/api/discogs_release_stream", methods=["GET"])
def api_discogs_release_stream():
    if not basic_auth_ok():
        return require_basic_auth()
    rid = (request.args.get("id") or "").strip()

    def generate():
        if not DISCOGS_TOKEN:
            yield sse_event("apierror", "DISCOGS_TOKEN not set.")
            return
        if not rid.isdigit():
            yield sse_event("apierror", "Provide a Discogs release id.")
            return
        yield sse_event("log", f"Fetching Discogs release {rid}\u2026")
        try:
            r = http_get(f"https://api.discogs.com/releases/{rid}",
                         timeout=25, headers={"Authorization": f"Discogs token={DISCOGS_TOKEN}"})
            r.raise_for_status()
            data = r.json()
            yield sse_event("log", "Processing release data\u2026")
            artists = data.get("artists", [])
            albumartist = artists[0].get("name", "") if artists else ""
            title = data.get("title", "") or ""
            year = str(data.get("year", "") or "")
            labels = data.get("labels", []) or []
            label = labels[0].get("name", "") if labels else ""
            catno = labels[0].get("catno", "") if labels else ""
            images = data.get("images", []) or []
            art_url = ""
            if images:
                prim = [i for i in images if i.get("type") == "primary"]
                art_url = (prim[0].get("uri") if prim else images[0].get("uri")) or ""
            tracklist = []
            for t in (data.get("tracklist", []) or []):
                tracklist.append({
                    "position": t.get("position", ""),
                    "title": t.get("title", ""),
                    "duration": t.get("duration", ""),
                })
            yield sse_event("log", f"Got {len(tracklist)} track(s) in tracklist.")
            yield sse_event("result", json.dumps({
                "fields": {
                    "album": title,
                    "albumartist": albumartist,
                    "year": year if year.isdigit() else "",
                    "label": label,
                    "catalog_number": catno,
                    "art_url": art_url,
                },
                "tracklist": tracklist,
            }))
        except Exception as e:
            yield sse_event("apierror", str(e))

    return sse_response(generate())

@app.route("/api/acoustid_stream", methods=["GET"])
def api_acoustid_stream():
    if not basic_auth_ok():
        return require_basic_auth()
    path_arg = request.args.get("path", "")

    def generate():
        try:
            if not ACOUSTID_KEY:
                yield sse_event("apierror", "ACOUSTID_KEY not set.")
                return
            try:
                path = safe_path(path_arg)
            except ValueError as exc:
                yield sse_event("apierror", str(exc))
                return
            yield sse_event("log", "Computing audio fingerprint\u2026")

            def do_match(pth):
                return acoustid.match(ACOUSTID_KEY, pth, meta="recordings releases", parse=False)

            try:
                results = do_match(path)
            except Exception as e:
                msg = str(e).lower()
                if "could not be decoded" in msg or "decode" in msg:
                    yield sse_event("log", "Decode error, trying ffmpeg fallback\u2026")
                    try:
                        with tempfile.TemporaryDirectory() as td:
                            wav = os.path.join(td, "tmp.wav")
                            yield sse_event("log", "Converting audio with ffmpeg\u2026")
                            proc = subprocess.run(
                                ["ffmpeg", "-y", "-v", "error", "-i", path, "-t", "60", "-ac", "2", "-ar", "44100", wav],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                            )
                            if proc.returncode != 0:
                                yield sse_event("apierror", f"ffmpeg error: {proc.stderr.strip()[:400]}")
                                return
                            yield sse_event("log", "Querying AcoustID\u2026")
                            results = do_match(wav)
                    except Exception as e2:
                        yield sse_event("apierror", f"AcoustID lookup failed after ffmpeg fallback: {e2}")
                        return
                else:
                    yield sse_event("apierror", f"AcoustID lookup failed: {e}")
                    return
            out = list(islice(_parse_acoustid_response(results), 10))
            yield sse_event("log", f"Found {len(out)} match(es).")
            yield sse_event("result", json.dumps({"results": out}))
        except Exception as e:
            # Keep SSE clients from seeing a bare "Connection lost" when an unexpected
            # exception escapes the normal AcoustID error handling path.
            yield sse_event("apierror", f"Unexpected AcoustID stream error: {e}")

    return sse_response(generate())

@app.route("/api/genres", methods=["GET"])
def api_genres():
    """Return the list of genre folder names (MUSIC_ROOT subdirs, excluding Downloads)."""
    if not basic_auth_ok():
        return require_basic_auth()
    return jsonify({"genres": _genre_folders()})

_MBID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

def _valid_mbid(s: str) -> bool:
    return bool(s and _MBID_RE.match(s))

@app.route("/api/suggest_genre", methods=["GET"])
def api_suggest_genre():
    """Suggest a genre folder using MusicBrainz tags for a recording or release MBID."""
    if not basic_auth_ok():
        return require_basic_auth()
    rec_id = (request.args.get("recording_id") or "").strip()
    rel_id = (request.args.get("release_id") or "").strip()
    if not rec_id and not rel_id:
        return jsonify({"error": "Provide recording_id or release_id."}), 400
    if rec_id and not _valid_mbid(rec_id):
        return jsonify({"error": "recording_id must be a valid UUID."}), 400
    if rel_id and not _valid_mbid(rel_id):
        return jsonify({"error": "release_id must be a valid UUID."}), 400
    try:
        mb_tags: list = []
        if rec_id:
            r = mb_get(
                f"https://musicbrainz.org/ws/2/recording/{rec_id}",
                params={"fmt": "json", "inc": "tags"},
            )
            r.raise_for_status()
            mb_tags = r.json().get("tags") or []
        if not mb_tags and rel_id:
            r = mb_get(
                f"https://musicbrainz.org/ws/2/release/{rel_id}",
                params={"fmt": "json", "inc": "tags"},
            )
            r.raise_for_status()
            mb_tags = r.json().get("tags") or []
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    tags_sorted = sorted(
        [{"name": t.get("name", ""), "count": t.get("count", 0)} for t in mb_tags if t.get("name")],
        key=lambda x: -x["count"],
    )
    folders = _genre_folders()
    matches = _map_tags_to_folders(tags_sorted, folders)
    return jsonify({"mb_tags": tags_sorted, "matches": matches, "genres": folders})

@app.route("/api/audio", methods=["GET"])
def api_audio():
    if not basic_auth_ok():
        return require_basic_auth()
    try:
        path = safe_path(request.args.get("path", ""))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    file_size = os.path.getsize(path)
    range_header = request.headers.get("Range", "")

    if range_header:
        # Parse Range header: bytes=START-END, bytes=START-, bytes=-SUFFIXLEN
        m = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if not m:
            return Response("Invalid Range", status=416,
                            headers={"Content-Range": f"bytes */{file_size}"})
        start_str, end_str = m.group(1), m.group(2)
        if start_str == "" and end_str == "":
            return Response("Invalid Range", status=416,
                            headers={"Content-Range": f"bytes */{file_size}"})
        if start_str == "":
            # bytes=-SUFFIXLEN
            suffix = int(end_str)
            start = max(0, file_size - suffix)
            end = file_size - 1
        elif end_str == "":
            # bytes=START-
            start = int(start_str)
            end = file_size - 1
        else:
            start = int(start_str)
            end = min(int(end_str), file_size - 1)
        if start > end or start >= file_size:
            return Response("Range Not Satisfiable", status=416,
                            headers={"Content-Range": f"bytes */{file_size}"})
        length = end - start + 1

        def generate_partial():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                chunk = 65536
                while remaining > 0:
                    data = f.read(min(chunk, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        }
        return Response(generate_partial(), status=206, mimetype="audio/mpeg",
                        headers=headers, direct_passthrough=True)

    # Non-range: return full file
    def generate_full():
        with open(path, "rb") as f:
            while True:
                data = f.read(65536)
                if not data:
                    break
                yield data

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
    }
    return Response(generate_full(), status=200, mimetype="audio/mpeg",
                    headers=headers, direct_passthrough=True)

@app.route("/", methods=["GET"])
def ui_home():
    if not basic_auth_ok():
        return require_basic_auth()

    # If path provided, open editor immediately
    path = (request.args.get("path") or "").strip()
    browse_default = _BROWSE_DEFAULT

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>MP3 Tag Editor</title>
  <link rel="icon" type="image/svg+xml" href="{_TAB_ICON_DATA_URL}"/>
  <style>
    :root {{
      --accent: #FF1A55;
      --accent-strong: #e10045;
      --accent-soft: #ffe6ee;
      --accent-focus: rgba(255,26,85,.20);
      --border: #e4e6eb;
      --bg: #f5f7fb;
      --card-bg: #ffffff;
      --card-alt: #fcfdff;
      --thumb-size: 44px;
      --text: #131822;
      --muted: #697386;
      --min-bg: #fff8e8;
      --min-border: #ffc145;
      --no-genre: #fff2f6;
      --radius: 14px;
      --radius-sm: 10px;
      --shadow: 0 8px 24px rgba(16, 24, 40, .07);
      --shadow-soft: 0 2px 8px rgba(16, 24, 40, .05);
    }}
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: system-ui, "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 20px;
      width: 100%;
      line-height: 1.4;
    }}
    h1 {{ font-size: 1.28rem; font-weight: 750; margin: 0 0 3px; letter-spacing: -.02em; }}
    h2 {{ font-size: 1.05rem; font-weight: 700; margin: 0 0 10px; color: var(--accent-strong); letter-spacing: -.01em; }}
    h3 {{ font-size: .95rem; font-weight: 600; margin: 0 0 8px; }}
    p.sub {{ color: var(--muted); font-size: .85rem; margin: 0 0 16px; }}
    .topbar {{
      margin-bottom: 12px;
      padding: 10px 12px;
      background: linear-gradient(135deg, #ffffff 0%, #fff5f8 100%);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow-soft);
    }}
    .topbar .sub {{ margin-bottom: 0; font-size: .76rem; }}
    .card {{
      background: linear-gradient(180deg, var(--card-bg) 0%, var(--card-alt) 100%);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px;
      margin-bottom: 16px;
      box-shadow: var(--shadow);
    }}
    .row2 {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }}
    .row2 > * {{ min-width: 0; }}
    @media(max-width:700px){{ .row2 {{ grid-template-columns: 1fr; }} }}
    .workspace {{ display:grid; grid-template-columns: minmax(320px, 430px) minmax(0, 1fr); gap:16px; align-items:start; }}
    @media(max-width:1100px){{ .workspace {{ grid-template-columns: 1fr; }} }}
    .left-pane {{ position: sticky; top: 14px; align-self: start; }}
    @media(max-width:1100px){{ .left-pane {{ position: static; }} }}
    label {{ display: block; font-size: .85rem; font-weight: 600; margin-bottom: 4px; }}
    input[type=text], input:not([type]), textarea {{
      width: 100%; padding: 11px 14px;
      border: 1px solid #dfe3ea; border-radius: 16px;
      font-size: .9rem; background: #fff; color: var(--text);
      margin-bottom: 10px; transition: border-color .15s, box-shadow .15s;
    }}
    input:focus, textarea:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 4px var(--accent-focus); }}
    input.dirty, textarea.dirty {{ color: #b91c1c; border-color: #b91c1c; }}
    input.dirty:focus, textarea.dirty:focus {{ box-shadow: 0 0 0 3px rgba(185,28,28,.15); }}
    textarea {{ resize: vertical; }}
    .hint {{ color: var(--muted); font-size: .78rem; margin: -8px 0 10px; }}
    .field-wrap {{ display: flex; align-items: flex-start; gap: 8px; }}
    .field-wrap > input, .field-wrap > textarea {{ flex: 1; min-width: 0; margin-bottom: 0; }}
    .revert-btn {{
      display: none; flex: 0 0 auto;
      padding: 2px 7px; font-size: .75rem; font-weight: 600; line-height: 1.4;
      border: 1px solid #b91c1c; border-radius: 4px; background: #fff; color: #b91c1c;
      cursor: pointer;
    }}
    .revert-btn:hover {{ background: #fef2f2; }}
    .field-wrap.dirty .revert-btn {{ display: inline-block; }}
    .old-value {{
      display: none; font-size: .75rem; color: var(--muted);
      margin: -10px 0 10px; padding: 2px 4px;
      word-break: break-all;
    }}
    .field-wrap.dirty ~ .old-value {{ display: block; }}
    .btn {{
      display: inline-block; padding: 9px 14px; font-size: .85rem; font-weight: 700;
      border: 1px solid transparent; border-radius: var(--radius-sm);
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent-strong) 100%);
      color: #fff;
      cursor: pointer; margin-right: 6px; margin-bottom: 6px;
      transition: transform .08s ease, filter .15s ease, box-shadow .15s ease;
      box-shadow: 0 4px 12px rgba(225, 0, 69, .28);
    }}
    .btn:hover {{ filter: brightness(1.04); }}
    .btn:active {{ transform: translateY(1px); }}
    .btn-sm {{ padding: 5px 10px; font-size: .8rem; }}
    .btn-outline {{ background: #fff; border: 1px solid var(--accent); color: var(--accent-strong); box-shadow: none; }}
    .btn-ghost {{ background: #fff; color: var(--text); border: 1px solid var(--border); box-shadow: none; }}
    .editor-main {{
      padding: 20px;
    }}
    .section-panel {{
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: #fff;
      padding: 14px;
      margin-bottom: 14px;
      box-shadow: var(--shadow-soft);
    }}
    .lookup-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0,1fr));
      gap: 12px;
    }}
    @media(max-width:1100px){{ .lookup-grid {{ grid-template-columns: 1fr; }} }}
    .lookup-stack {{ display:grid; gap:12px; }}
    .lookup-card {{
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: #fff;
      padding: 12px;
    }}
    .mono {{ font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; font-size: .82rem; }}
    .callout-min {{
      border: 1px solid var(--border); background: linear-gradient(180deg, #ffffff 0%, #fff8fb 100%);
      border-radius: var(--radius); padding: 16px; margin-bottom: 16px;
      box-shadow: var(--shadow-soft);
    }}
    .callout-min .callout-title {{
      font-size: .78rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: .08em; color: var(--accent-strong); margin-bottom: 12px;
    }}

    .field-grid-2 {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }}
    .field-grid-3 {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }}
    .field-card {{ border:1px solid var(--border); border-radius:16px; padding:12px; background:#fff; margin-bottom:12px; }}
    .field-card h4 {{ margin:0 0 10px; font-size:.82rem; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }}
    .quick-actions {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }}
    .chip-btn {{ border:1px solid #ffd2df; background:#fff6f9; color:var(--accent-strong); border-radius:999px; padding:5px 10px; font-size:.74rem; font-weight:700; cursor:pointer; }}
    .chip-btn:hover {{ background:#ffeef4; }}
    .inline-check {{ display:flex; align-items:center; gap:8px; margin-top:4px; font-size:.8rem; color:var(--muted); }}
    .inline-check input {{ width:auto; margin:0; }}
    @media(max-width:900px){{ .field-grid-2, .field-grid-3 {{ grid-template-columns:1fr; }} }}
    .file-list {{
      max-height: 380px; overflow-y: auto;
      border: 1px solid var(--border); border-radius: var(--radius); background: #fff;
    }}
    .file-item {{
      display: flex; align-items: flex-start; gap: 10px; padding: 8px 10px;
      cursor: pointer; border-bottom: 1px solid #f0f0f0; transition: background .1s;
    }}
    .file-item-select {{ margin-top: 3px; }}
    .file-item:last-child {{ border-bottom: none; }}
    .file-item:hover {{ background: #fff0f5; }}
    .file-item.selected {{ background: var(--accent-soft); }}
    .file-item.no-genre {{ background: var(--no-genre); }}
    .file-item.no-genre:hover {{ background: #ffe4e4; }}
    .file-item.no-genre.selected {{ background: var(--accent-soft); }}
    .file-thumb {{ width: var(--thumb-size); height: var(--thumb-size); border-radius: 6px; object-fit: cover; flex-shrink: 0; }}
    .file-thumb-placeholder {{
      width: var(--thumb-size); height: var(--thumb-size); border-radius: 6px; background: #e5e7eb;
      flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: .64rem;
      font-weight: 700; letter-spacing: .04em; color: #4b5563;
    }}
    .file-meta {{ flex: 1; min-width: 0; }}
    .file-name {{ font-weight: 600; font-size: .88rem; white-space: normal; overflow-wrap: anywhere; word-break: break-word; }}
    .file-path {{ font-size: .75rem; color: var(--muted); white-space: normal; overflow-wrap: anywhere; word-break: break-word; margin-top: 1px; }}
    .file-artist {{ font-size: .82rem; margin-top: 2px; }}
    .file-title-tag {{ font-size: .82rem; color: var(--muted); margin-top: 1px; }}
    .file-footer {{ display: flex; gap: 8px; margin-top: 3px; font-size: .78rem; }}
    .genre-badge {{ background: #d1fae5; color: #065f46; border-radius: 999px; padding: 1px 7px; font-size: .75rem; font-weight: 600; }}
    .genre-missing {{ background: #fee2e2; color: #991b1b; border-radius: 999px; padding: 1px 7px; font-size: .75rem; font-weight: 600; }}
    .dir-item {{
      display: flex; align-items: center; gap: 8px; padding: 7px 10px;
      cursor: pointer; border-bottom: 1px solid #f0f0f0; font-size: .88rem; font-weight: 500;
    }}
    .dir-item:hover {{ background: #fff0f5; }}
    details.mb-section summary {{
      cursor: pointer; font-size: .95rem; font-weight: 600; color: var(--accent);
      padding: 8px 0; user-select: none; list-style: disclosure-closed;
    }}
    details.mb-section[open] summary {{ margin-bottom: 12px; list-style: disclosure-open; }}
    .result-item {{ border: 1px solid var(--border); border-radius: 8px; padding: 10px; margin-bottom: 8px; }}
    .match-badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:.72rem; font-weight:700; margin-left:7px; }}
    .match-high {{ background:#d1fae5; color:#065f46; }}
    .match-medium {{ background:#fef3c7; color:#92400e; }}
    .match-low {{ background:#fee2e2; color:#991b1b; }}
    .bulk-tools {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:8px 0; }}
    .bulk-tools input {{ width:auto; min-width:90px; margin-bottom:0; }}
    .input-inline {{ display:flex; gap:8px; align-items:center; }}
    .input-inline input {{ margin-bottom:0; }}
    .input-inline .btn {{ margin-right:0; margin-bottom:0; white-space:nowrap; }}
    .bulk-count {{ font-size:.78rem; color:var(--muted); }}
    .sticky-actions {{ position: sticky; bottom: 10px; z-index: 5; background: rgba(255,255,255,.96); border:1px solid var(--border); border-radius:var(--radius); padding:10px; margin-top:10px; box-shadow: var(--shadow); }}
    .field-error {{ color:#991b1b; font-size:.76rem; margin:-8px 0 8px; display:none; }}
    .invalid {{ border-color:#991b1b !important; background:#fff1f2; }}
    #toastStack {{ position:fixed; right:14px; bottom:14px; display:flex; flex-direction:column; gap:8px; z-index:2000; }}
    .toast {{ color:#fff; padding:10px 12px; border-radius:8px; font-size:.84rem; box-shadow:0 6px 18px rgba(0,0,0,.2); max-width:340px; }}
    .toast.success {{ background:#065f46; }}
    .toast.error {{ background:#991b1b; }}
    .toast.info {{ background:#1f2937; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .section-sep {{ border: none; border-top: 1px solid var(--border); margin: 16px 0; }}
    .field-group {{ margin-bottom: 0; }}
    #resultModal {{
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5);
      z-index: 2100; align-items: center; justify-content: center;
    }}
    #resultModal.open {{ display: flex; }}
    .modal-inner {{
      background: var(--card-bg); border-radius: var(--radius); padding: 24px;
      max-width: 540px; width: 90%; box-shadow: 0 8px 32px rgba(0,0,0,.25);
    }}
    .stream-status {{
      font-size: .8rem; font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
      padding: 5px 9px; border-radius: var(--radius-sm); background: #f8fafc;
      border: 1px solid var(--border); margin: 4px 0 8px;
      max-height: 110px; overflow-y: auto; display: none;
    }}
    .stream-status.active {{ display: block; }}
    .stream-status .log-line {{ color: var(--muted); }}
    .stream-status .s-ok {{ color: #065f46; font-weight: 600; }}
    .stream-status .s-err {{ color: #991b1b; font-weight: 600; }}
    .picker-list {{ max-height: 300px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; background: #fff; margin-top: 8px; }}
    .picker-item {{ padding: 8px 10px; border-bottom: 1px solid #f0f0f0; cursor: pointer; font-size: .86rem; }}
    .picker-item:hover {{ background: #fff0f5; }}
  </style>
</head>
<body>

<div class="topbar">
  <h1>MP3 Tag Editor</h1>
  <p class="sub">Music root: <span class="mono">{MUSIC_ROOT}</span></p>
</div>

<div class="workspace">
  <div class="left-pane">
  <div class="card">
    <h2>Browse</h2>
    <p class="sub">Navigate folders. <strong>Double-click</strong> a file to load it for editing.</p>
    <label>Directory</label>
    <div class="input-inline">
      <input id="dir" value="{browse_default}"/>
      <button class="btn btn-ghost" type="button" onclick="openDirectoryPicker()">Browse…</button>
    </div>
    <label>Filter (optional)</label>
    <input id="dirFilter" placeholder="e.g. maribou or 2024"/>
    <button class="btn" type="button" onclick="loadDir()">Load</button>
    <button class="btn btn-ghost" type="button" onclick="upDir()">Up</button>
    <div class="bulk-tools">
      <button type="button" class="btn btn-sm btn-ghost" onclick="toggleVisibleListSelections('dirList', true)">Select all</button>
      <button type="button" class="btn btn-sm btn-ghost" onclick="toggleVisibleListSelections('dirList', false)">Clear</button>
    </div>
    <div id="dirErr" class="hint"></div>
    <div class="file-list" id="dirList"></div>
  </div>

  <div class="card">
    <h2>Search</h2>
    <p class="sub">Search recursively. <strong>Double-click</strong> a file to load it.</p>
    <label>Search root</label>
    <div class="input-inline">
      <input id="sroot" value="{MUSIC_ROOT}"/>
      <button class="btn btn-ghost" type="button" onclick="openDirectoryPicker('sroot')">Browse…</button>
    </div>
    <label>Query</label>
    <input id="sq" placeholder="filename or partial path"/>
    <button class="btn" type="button" onclick="doSearch()">Search</button>
    <div class="bulk-tools">
      <button type="button" class="btn btn-sm btn-ghost" onclick="toggleVisibleListSelections('sList', true)">Select all</button>
      <button type="button" class="btn btn-sm btn-ghost" onclick="toggleVisibleListSelections('sList', false)">Clear</button>
    </div>
    <div id="sErr" class="hint"></div>
    <div class="file-list" id="sList"></div>
  </div>

<div class="card" id="playerCard">
  <h2>Player</h2>
  <div id="playerFileName" style="font-size:.88rem;color:var(--muted);margin-bottom:10px">No file loaded.</div>
  <audio id="audioPlayer" controls style="width:100%;display:none"></audio>
</div>

<div class="card">
  <h3>Bulk apply to selected files</h3>
  <div class="bulk-count" id="bulkCount">0 selected</div>
  <div class="bulk-tools">
    <input id="bulkGenre" placeholder="Genre"/>
    <input id="bulkAlbum" placeholder="Album"/>
    <input id="bulkYear" placeholder="Year"/>
    <button type="button" class="btn btn-sm" onclick="applyBulkEdits()">Apply</button>
  </div>
</div>
  </div>

<div class="right-pane">
<div class="card editor-main">
  <h2>Edit Tags</h2>
  <p class="sub">Load a file, optionally use lookups, then write. Archive reorganises to <span class="mono">{MUSIC_ROOT}/Genre/AlbumArtist/Album [Year]/</span>.</p>

  <form id="tagForm" method="POST" action="/update">
    <input type="hidden" name="path" id="path" value="{path}"/>
    <div id="currentFile" class="hint" style="margin-bottom:8px;font-size:.88rem">Double-click a file in Browse or Search to load it for editing.</div>
    <pre id="clickDebug" class="hint" style="margin:0 0 10px 0;font-size:.75rem;display:none;max-height:120px;overflow:auto;white-space:pre-wrap"></pre>
    <div id="loadMsg" class="hint"></div>

    <hr class="section-sep"/>

    <div class="section-panel">
      <h3>Lookups</h3>
      <div class="lookup-grid">
        <div class="lookup-card">
          <input id="wsq" placeholder="Web search: e.g. Artist – Title"/>
          <button type="button" class="btn btn-outline" onclick="webSearch()">Web Search (Beatport / Traxsource / Bandcamp / Juno)</button>
          <div id="webSearchStatus" class="stream-status"></div>
          <div id="webSearchResults"></div>
          <hr class="section-sep"/>
          <input id="purl" placeholder="Paste URL: Beatport / Bandcamp / Juno / Traxsource"/>
          <button type="button" class="btn btn-outline" onclick="parseUrl()">Parse URL</button>
          <div id="parseResults"></div>
        </div>
        <div class="lookup-stack">
          <div class="lookup-card">
            <button type="button" class="btn btn-outline" onclick="mbSearchDialog()">MusicBrainz Search&hellip;</button>
            <div id="mbResults"></div>
          </div>
          <div class="lookup-card">
            <button type="button" class="btn btn-outline" onclick="acoustid()">AcoustID Fingerprint</button>
            <div id="acoustidStatus" class="stream-status"></div>
            <p id="acoustidKeyStatus" class="sub" style="margin-top:4px"></p>
            <div id="acoustidResults"></div>
            <div id="acoustidMbStatus" class="hint"></div>
          </div>
          <div class="lookup-card">
            <button type="button" class="btn btn-outline" onclick="discogsSearch()">Discogs Search (album)</button>
            <div id="discogsStatus" class="stream-status"></div>
            <p id="discogsKeyStatus" class="sub" style="margin-top:4px"></p>
            <div id="discogsResults"></div>
            <div id="discogsTracklist"></div>
          </div>
        </div>
      </div>
    </div>

    <hr class="section-sep"/>

    <div class="callout-min">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div class="callout-title" style="margin-bottom:0">Minimum required tags</div>
        <button type="button" class="btn btn-sm btn-ghost" onclick="revertTags()" title="Reload tags from disk, restore fields and clear lookup results">Revert</button>
      </div>
      <div class="field-card">
        <h4>Core details</h4>
        <div class="field-grid-2">
          <div class="field-group"><label>Title</label><input name="title"/></div>
          <div class="field-group"><label>Artist</label><input name="artist"/></div>
          <div class="field-group"><label>Album</label><input name="album"/></div>
          <div class="field-group"><label>Album Artist (band)</label><input name="albumartist"/></div>
        </div>
        <div class="field-group">
          <label>Involved people list</label>
          <input name="involved_people_list" placeholder="DJ Jazzy Jeff, The Fresh Prince"/>
          <div class="hint">Stored as <span class="mono">TIPL</span> and <span class="mono">TXXX:involved_people_list</span></div>
        </div>
      </div>

      <div class="field-card">
        <h4>Release data</h4>
        <div class="field-grid-3">
          <div class="field-group"><label>Date <button type="button" class="chip-btn" onclick="copyYearToDate()">Set date from year</button></label><input name="date" placeholder="YYYY or YYYY-MM-DD"/></div>
          <div class="field-group"><label>Year <button type="button" class="chip-btn" onclick="copyDateToYear()">Set year from date</button></label><input name="year" placeholder="YYYY"/></div>
          <div class="field-group"><label>Original Year <button type="button" class="chip-btn" onclick="copyYearToOriginalYear()">Set orig. year from year</button></label><input name="original_year" placeholder="YYYY"/></div>
        </div>
        <div class="field-grid-3">
          <div class="field-group">
            <label>Genre</label>
            <input name="genre"/>
            <div id="genreSuggestions" style="margin-top:4px"></div>
          </div>
          <div class="field-group"><label>Track number <button type="button" class="chip-btn" onclick="normalizeTrackFormat()">Normalize track</button></label><input name="track" placeholder="1/0"/></div>
        </div>
        <div class="field-grid-3">
          <div class="field-group"><label>BPM</label><input name="bpm" placeholder="am (optional)"/></div>
        </div>
        <div class="hint">BPM stored as <span class="mono">TBPM</span> (decimals truncated; non-numeric ignored)</div>
      </div>

      <div class="field-card">
        <h4>Sort and credits</h4>
        <div class="field-grid-2">
          <div class="field-group">
            <label>Artist sort</label>
            <input name="artist_sort" placeholder="Pharcyde, The or Morales, David"/>
            <label class="inline-check"><input type="checkbox" id="isNameArtist" onchange="applyIsName('artist','artist_sort',this)"/> Name?</label>
          </div>
          <div class="field-group">
            <label>Album artist sort</label>
            <input name="albumartist_sort" placeholder="Pharcyde, The or Morales, David"/>
            <label class="inline-check"><input type="checkbox" id="isNameAlbumartist" onchange="applyIsName('albumartist','albumartist_sort',this)"/> Name?</label>
          </div>
        </div>
      </div>

      <div class="field-card">
        <h4>Publishing and notes</h4>
        <div class="field-grid-2">
          <div class="field-group">
            <label>Catalog number</label><input name="catalog_number" placeholder="(optional)"/>
            <div class="hint">Stored as <span class="mono">TXXX:CATALOGNUMBER</span></div>
          </div>
        </div>
        <div class="field-grid-2">
          <div class="field-group"><label>Publisher</label><input name="publisher" placeholder="(optional)"/></div>
          <div class="field-group"><label>Comment</label><textarea name="comment" rows="3" placeholder="(optional)"></textarea></div>
        </div>
      </div>

      <div class="field-card">
        <h4>Artwork</h4>
        <div class="field-group">
          <label>Cover art image URL</label>
          <div style="display:flex;gap:12px;align-items:flex-start">
            <div style="flex:1;min-width:0">
              <input name="art_url" id="art_url_field" placeholder="https://.../cover.jpg (embedded as JPEG)" style="margin-bottom:0" onblur="checkArtUrlDim()"/>
              <div id="artUrlDims" class="hint" style="margin-top:6px"></div>
            </div>
            <div id="artPreview" style="display:none;flex-shrink:0;text-align:center">
              <img id="artImg" src="" style="max-width:120px;max-height:120px;border-radius:10px;cursor:pointer;display:block" alt="Embedded artwork" onclick="showArtModal(this.src)"/>
              <div id="artDims" class="hint" style="margin-top:3px;margin-bottom:0"></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <details class="mb-section card" style="margin-bottom:16px">
      <summary>Advanced / MusicBrainz Picard fields</summary>
      <div class="row2">
        <div>
          <div class="field-group"><label>MusicBrainz Track ID</label><input name="musicbrainz_trackid" class="mono" placeholder="UUID"/></div>
          <div class="field-group"><label>MusicBrainz Album ID</label><input name="musicbrainz_albumid" class="mono" placeholder="UUID"/></div>
          <div class="field-group"><label>MusicBrainz Release Group ID</label><input name="musicbrainz_releasegroupid" class="mono" placeholder="UUID"/></div>
          <div class="field-group"><label>MusicBrainz Artist ID</label><input name="musicbrainz_artistid" class="mono" placeholder="UUID"/></div>
          <div class="field-group"><label>MusicBrainz Album Artist ID</label><input name="musicbrainz_albumartistid" class="mono" placeholder="UUID"/></div>
        </div>
        <div>
          <div class="field-group"><label>MusicBrainz Release Country</label><input name="musicbrainz_releasecountry" placeholder="e.g. GB"/></div>
          <div class="field-group"><label>MusicBrainz Release Status</label><input name="musicbrainz_releasestatus" placeholder="Official / Promotion&hellip;"/></div>
          <div class="field-group"><label>MusicBrainz Release Type</label><input name="musicbrainz_releasetype" placeholder="Album / Single&hellip;"/></div>
          <div class="field-group"><label>MusicBrainz Album Type</label><input name="musicbrainz_albumtype" placeholder="Album / Single&hellip;"/></div>
          <div class="field-group"><label>MusicBrainz Album Status</label><input name="musicbrainz_albumstatus" placeholder="Official&hellip;"/></div>
          <div class="field-group"><label>MusicBrainz Album Artist</label><input name="musicbrainz_albumartist"/></div>
          <div class="field-group"><label>MusicBrainz Artist</label><input name="musicbrainz_artist"/></div>
          <div class="field-group"><label>MusicBrainz Album</label><input name="musicbrainz_album"/></div>
          <div class="field-group"><label>Barcode</label><input name="barcode" placeholder="EAN/UPC"/></div>
        </div>
      </div>
    </details>

    <div class="sticky-actions">
      <button type="button" class="btn btn-ghost" onclick="revertTags()" title="Reload tags from disk">Discard unsaved edits</button>
      <button class="btn" type="submit" name="action" value="write">Write tags</button>
      <button class="btn btn-ghost" type="submit" name="action" value="archive">Write tags + Archive</button>
    </div>
  </form>
</div>
</div>
</div>

<div id="resultModal">
  <div class="modal-inner">
    <div id="resultModalBody"></div>
    <button type="button" class="btn" style="margin-top:16px" onclick="closeModal()">Close</button>
  </div>
</div>

<div id="dirPickerModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1001;align-items:center;justify-content:center">
  <div class="modal-inner" style="max-height:90vh;overflow-y:auto">
    <h3 style="margin-top:0">Choose directory</h3>
    <div id="dirPickerPath" class="hint mono" style="margin-bottom:4px"></div>
    <div class="bulk-tools" style="margin-top:0">
      <button type="button" class="btn btn-sm btn-ghost" onclick="dirPickerUp()">Up</button>
      <button type="button" class="btn btn-sm" onclick="confirmDirectoryPicker()">Use this directory</button>
      <button type="button" class="btn btn-sm btn-ghost" onclick="closeDirectoryPicker()">Cancel</button>
    </div>
    <div id="dirPickerErr" class="hint"></div>
    <div id="dirPickerList" class="picker-list"></div>
  </div>
</div>

<div id="mbDialog" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1001;align-items:center;justify-content:center">
  <div style="background:var(--card-bg);border-radius:var(--radius);padding:24px;max-width:520px;width:92%;box-shadow:0 8px 32px rgba(0,0,0,.3);max-height:90vh;overflow-y:auto">
    <h3 style="margin:0 0 14px">MusicBrainz Search</h3>
    <label>Title</label><input id="mbDlgTitle" placeholder="Track title"/>
    <label>Artist</label><input id="mbDlgArtist" placeholder="Artist name"/>
    <label>Album / Release</label><input id="mbDlgAlbum" placeholder="Album or release (optional)"/>
    <label>Year</label><input id="mbDlgYear" placeholder="Year e.g. 2018 (optional)"/>
    <div style="display:flex;gap:8px;margin-bottom:10px">
      <button type="button" class="btn" onclick="runMbSearch()">Search</button>
      <button type="button" class="btn btn-ghost" onclick="closeMbDialog()">Cancel</button>
    </div>
    <div id="mbDlgStatus" class="hint" style="margin-bottom:4px"></div>
    <div id="mbSearchStatus" class="stream-status" style="margin-bottom:8px"></div>
    <div id="mbDlgResults"></div>
  </div>
</div>

<div id="toastStack" aria-live="polite" aria-atomic="true"></div>

<script>
let _baseline = {{}};

function ensureRevertUIForField(name) {{
  const el = document.querySelector(`[name="${{name}}"]`);
  if(!el || el.dataset.revertUiDone) return;
  el.dataset.revertUiDone = "1";
  const wrap = document.createElement("div");
  wrap.className = "field-wrap";
  el.parentNode.insertBefore(wrap, el);
  wrap.appendChild(el);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "revert-btn";
  btn.title = "Revert to saved value";
  btn.textContent = "\u21a9 Revert";
  btn.dataset.revertFor = name;
  wrap.appendChild(btn);
  const saved = document.createElement("div");
  saved.className = "old-value";
  saved.dataset.oldValueFor = name;
  wrap.insertAdjacentElement("afterend", saved);
}}

function updateRevertUI(name) {{
  const el = document.querySelector(`[name="${{name}}"]`);
  if(!el) return;
  const wrap = el.closest(".field-wrap");
  const saved = document.querySelector(`[data-old-value-for="${{name}}"]`);
  const isDirty = (name in _baseline) && el.value !== (_baseline[name] || "");
  if(wrap) {{
    if(isDirty) wrap.classList.add("dirty");
    else wrap.classList.remove("dirty");
  }}
  if(saved) {{
    if(isDirty) {{ saved.style.display = "block"; saved.textContent = "Saved: " + (_baseline[name] || "(empty)"); }}
    else {{ saved.style.display = "none"; saved.textContent = ""; }}
  }}
}}

function updateAllRevertUI() {{
  for(const k of Object.keys(_baseline)) updateRevertUI(k);
}}

function setBaseline(data) {{
  const keys = TAG_FIELDS;
  _baseline = {{}};
  for(const k of keys) _baseline[k] = data && data[k] !== undefined ? (data[k] || "") : (getField(k) || "");
  document.querySelectorAll("#tagForm input.dirty, #tagForm textarea.dirty").forEach(el => el.classList.remove("dirty"));
  for(const k of keys) ensureRevertUIForField(k);
  updateAllRevertUI();
}}
function esc(s){{ return (s||"").toString().replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;"); }}
function showToast(message, kind="info", ms=2800) {{
  const stack = document.getElementById("toastStack");
  if(!stack) return;
  const el = document.createElement("div");
  el.className = `toast ${{kind}}`;
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => el.remove(), ms);
}}
function confidenceBadge(score) {{
  const v = Number(score || 0);
  if(v >= 85) return '<span class="match-badge match-high">High · ' + Math.round(v) + '</span>';
  if(v >= 65) return '<span class="match-badge match-medium">Medium · ' + Math.round(v) + '</span>';
  return '<span class="match-badge match-low">Low · ' + Math.round(v) + '</span>';
}}
function inlineJsString(s) {{
  return JSON.stringify(String(s || ""));
}}
function getSelectedPaths() {{
  return Array.from(document.querySelectorAll('.file-item-select:checked')).map(el => el.dataset.path || "").filter(Boolean);
}}
function updateBulkCount() {{
  const el = document.getElementById("bulkCount");
  if(el) el.textContent = `${{getSelectedPaths().length}} selected`;
}}
function toggleVisibleListSelections(listId, selected) {{
  const root = document.getElementById(listId);
  if(!root) return;
  root.querySelectorAll('.file-item-select').forEach(cb => cb.checked = selected);
  updateBulkCount();
}}
async function applyBulkEdits() {{
  const paths = getSelectedPaths();
  if(!paths.length) {{ showToast("Select at least one file first.", "error"); return; }}
  const genre = document.getElementById("bulkGenre").value.trim();
  const album = document.getElementById("bulkAlbum").value.trim();
  const year = document.getElementById("bulkYear").value.trim();
  if(!genre && !album && !year) {{ showToast("Enter at least one bulk field.", "error"); return; }}
  let done = 0;
  for(const p of paths) {{
    const res = await fetch(`/api/load?path=${{encodeURIComponent(p)}}`);
    const data = await res.json();
    if(!res.ok) continue;
    const fd = new FormData();
    fd.set("path", p);
    const keys = TAG_FIELDS;
    for(const k of keys) fd.set(k, data[k] || "");
    if(genre) fd.set("genre", genre);
    if(album) fd.set("album", album);
    if(year) fd.set("year", year);
    fd.set("action", "write");
    const ures = await fetch("/update", {{method:"POST", body:fd}});
    if(ures.ok) done += 1;
  }}
  showToast(`Bulk update complete: ${{done}}/${{paths.length}} files`, done ? "success" : "error", 3600);
  await loadDir();
  await doSearch();
}}
function setInlineFieldError(name, msg="") {{
  const el = document.querySelector(`[name="${{name}}"]`);
  if(!el) return;
  let err = el.parentElement.querySelector('.field-error');
  if(!err) {{
    err = document.createElement('div');
    err.className = 'field-error';
    el.insertAdjacentElement('afterend', err);
  }}
  if(msg) {{
    el.classList.add('invalid');
    err.textContent = msg;
    err.style.display = 'block';
  }} else {{
    el.classList.remove('invalid');
    err.textContent = '';
    err.style.display = 'none';
  }}
}}
function validateBeforeSave() {{
  let ok = true;
  const required = [["title","Title is required"],["artist","Artist is required"],["album","Album is required"],["genre","Genre is required"],["year","Year is required"]];
  for(const [k,msg] of required) {{
    const v = getField(k).trim();
    if(!v) {{ setInlineFieldError(k, msg); ok = false; }} else setInlineFieldError(k, "");
  }}
  return ok;
}}
function setField(name, val){{
  const el = document.querySelector(`[name="${{name}}"]`);
  if(!el) return;
  el.value = val || "";
  if(name in _baseline) {{
    if(el.value !== (_baseline[name] || "")) el.classList.add("dirty");
    else el.classList.remove("dirty");
  }}
  updateRevertUI(name);
}}
function getField(name){{ const el = document.querySelector(`[name="${{name}}"]`); return el ? el.value : ""; }}

function sortName(name) {{
  name = (name || "").trim();
  if(!name) return "";
  if(name.toLowerCase().startsWith("the ")) {{
    const rest = name.substring(4).trim();
    return rest ? rest + ", The" : name;
  }}
  return name;
}}

function isNameTransform(name) {{
  name = (name || "").trim();
  if(!name || name.includes(",")) return name;
  const parts = name.split(/\s+/);
  if(parts.length < 2) return name;
  const given = parts[0];
  const surname = parts.slice(1).join(" ");
  return surname + ", " + given;
}}


function copyYearToDate() {{
  const year = getField("year").trim();
  if(/^\d{4}$/.test(year)) setField("date", year);
}}

function copyDateToYear() {{
  const dateVal = getField("date").trim();
  const m = dateVal.match(/(\d{4})/);
  if(m) setField("year", m[1]);
}}

function copyYearToOriginalYear() {{
  const year = getField("year").trim();
  if(/^\d{{4}}$/.test(year)) setField("original_year", year);
}}

function normalizeTrackFormat() {{
  const raw = getField("track").trim();
  if(!raw) {{
    setField("track", "1/1");
    return;
  }}
  const parts = raw.split("/").map(p => p.trim());
  const left = parseInt(parts[0] || "", 10);
  const right = parseInt(parts[1] || "", 10);
  if(Number.isFinite(left) && Number.isFinite(right)) setField("track", `${{left}}/${{right}}`);
  else if(Number.isFinite(left)) setField("track", `${{left}}/1`);
  else setField("track", "1/1");
}}
function applyIsName(fieldName, sortFieldName, cb) {{
  if(!cb.checked) return;
  const el = document.querySelector(`[name="${{fieldName}}"]`);
  if(!el || !el.value.trim()) {{ cb.checked = false; return; }}
  // Populate the sort field from the source; leave the source field unchanged
  setField(sortFieldName, isNameTransform(el.value));
}}

function renderFileItem(it, idx){{
  const noGenre = it.type === "file" && !it.genre;
  const genreBadge = it.type === "file"
    ? (it.genre ? `<span class="genre-badge">${{esc(it.genre)}}</span>` : `<span class="genre-missing">no genre</span>`)
    : "";
  if(it.type === "dir"){{
    return `<div class="dir-item" data-idx="${{idx}}">
      <div class="file-thumb-placeholder">DIR</div><span>${{esc(it.name)}}</span>
    </div>`;
  }}
  const thumb = it.has_art
    ? `<img class="file-thumb" src="/api/art?path=${{encodeURIComponent(it.path)}}" alt="" loading="lazy" onerror="this.outerHTML='<div class=file-thumb-placeholder>ART</div>'">`
    : `<div class="file-thumb-placeholder">ART</div>`;
  return `<div class="file-item${{noGenre ? ' no-genre' : ''}}" data-idx="${{idx}}" data-type="${{esc(it.type || 'file')}}" data-path="${{esc(it.path)}}">
    <input type="checkbox" class="file-item-select" data-path="${{esc(it.path)}}" title="Select for bulk apply" onclick="event.stopPropagation()"/>
    ${{thumb}}
    <div class="file-meta">
      <div class="file-name">${{esc(it.name)}}</div>
      <div class="file-path mono">${{esc(it.rel)}}</div>
      <div class="file-artist">${{esc(it.artist||"")}}</div>
      <div class="file-title-tag">${{esc(it.title||"")}}</div>
      <div class="file-footer"><span style="color:var(--muted)">${{esc(it.date||"")}}</span>${{genreBadge}}</div>
    </div>
  </div>`;
}}

async function loadDir(){{
  const d = document.getElementById("dir").value.trim();
  const q = document.getElementById("dirFilter").value.trim();
  const err = document.getElementById("dirErr");
  const box = document.getElementById("dirList");
  err.textContent = "Loading…";
  const res = await fetch(`/api/list?dir=${{encodeURIComponent(d)}}&q=${{encodeURIComponent(q)}}`);
  const data = await res.json();
  if(!res.ok){{ err.textContent = data.error || "Error"; box.innerHTML=""; return; }}
  err.textContent = "";
  document.getElementById("dir").value = data.dir;
  _dirItems = data.items;
  box.innerHTML = data.items.map((it,i) => renderFileItem(it,i)).join("");
  updateBulkCount();
}}

function openDir(p){{ document.getElementById("dir").value = p; loadDir(); }}
let _dirPickerPath = "";
let _dirPickerTarget = "dir";
function closeDirectoryPicker() {{
  const m = document.getElementById("dirPickerModal");
  if(m) m.style.display = "none";
}}
async function openDirectoryPicker(targetId = "dir") {{
  _dirPickerTarget = targetId || "dir";
  const targetInput = document.getElementById(_dirPickerTarget);
  _dirPickerPath = (targetInput ? targetInput.value.trim() : "") || "{MUSIC_ROOT}";
  const m = document.getElementById("dirPickerModal");
  if(m) m.style.display = "flex";
  await loadDirectoryPicker(_dirPickerPath);
}}
async function loadDirectoryPicker(dirPath) {{
  const err = document.getElementById("dirPickerErr");
  const list = document.getElementById("dirPickerList");
  const pathEl = document.getElementById("dirPickerPath");
  if(err) err.textContent = "Loading…";
  const res = await fetch(`/api/list?dir=${{encodeURIComponent(dirPath)}}`);
  const data = await res.json();
  if(!res.ok) {{ if(err) err.textContent = data.error || "Unable to load directory."; return; }}
  _dirPickerPath = data.dir;
  if(pathEl) pathEl.textContent = data.dir;
  if(err) err.textContent = "";
  const dirs = (data.items || []).filter(it => it.type === "dir");
  if(!list) return;
  list.innerHTML = dirs.length
    ? dirs.map(d => `<div class="picker-item" data-path="${{esc(d.path)}}">📁 ${{esc(d.name)}}</div>`).join("")
    : '<div class="picker-item" style="cursor:default;color:var(--muted)">No subdirectories</div>';
  list.querySelectorAll('.picker-item[data-path]').forEach(el => el.addEventListener('click', () => loadDirectoryPicker(el.dataset.path)));
}}
function dirPickerUp() {{
  const parts = (_dirPickerPath || "").split("/").filter(Boolean);
  if(parts.length <= 1) return;
  parts.pop();
  loadDirectoryPicker("/" + parts.join("/"));
}}
function confirmDirectoryPicker() {{
  if(!_dirPickerPath) return;
  const targetInput = document.getElementById(_dirPickerTarget || "dir");
  if(targetInput) targetInput.value = _dirPickerPath;
  closeDirectoryPicker();
  if((_dirPickerTarget || "dir") === "dir") loadDir();
}}

function openFile(p){{
  document.getElementById("path").value = p;
  const fn = p.split("/").pop();
  const cf = document.getElementById("currentFile");
  if(cf) cf.textContent = `Selected: ${{fn}} \u2014 double-click to load`;
  // highlight selected item in dir list
  document.querySelectorAll(".file-item.selected").forEach(el => el.classList.remove("selected"));
  document.querySelectorAll(`[data-path="${{CSS.escape(p)}}"]`).forEach(el => el.classList.add("selected"));
  // Update web search query with filename (unless user manually edited since last auto-set)
  const wsqEl = document.getElementById("wsq");
  if(wsqEl && (!wsqEl.value.trim() || wsqEl.value.trim() === _wsqAutoValue.trim())){{
    const autoVal = fn.replace(/\\.mp3$/i, "");
    wsqEl.value = autoVal;
    _wsqAutoValue = autoVal;
  }}
}}

async function loadFileByPath(p){{
  const seq = ++_activeLoad.seq;
  _activeLoad.path = p;
  document.getElementById("path").value = p;
  const fn = p.split("/").pop();
  const cf = document.getElementById("currentFile");
  if(cf) cf.textContent = `Loading: ${{fn}}\u2026`;
  const loaded = await loadTags(p, seq);
  if(!loaded) return;
  showToast(`Loaded ${{fn}}`, "info", 1400);
  // Load audio player
  const audio = document.getElementById("audioPlayer");
  const playerFn = document.getElementById("playerFileName");
  if(audio){{
    audio.src = `/api/audio?path=${{encodeURIComponent(p)}}`;
    audio.style.display = "";
    audio.load();
  }}
  if(playerFn) playerFn.textContent = fn;
  // Scroll to Edit Tags section
  const editH2 = Array.from(document.querySelectorAll("h2")).find(h => h.textContent.trim() === "Edit Tags");
  if(editH2) editH2.scrollIntoView({{behavior: "smooth", block: "start"}});
}}

function clearLookupResults() {{
  window._webResults = [];
  window._ac = [];
  window._mb = [];
  [
    "mbResults",
    "discogsResults",
    "discogsTracklist",
    "parseResults",
    "acoustidResults",
    "genreSuggestions",
  ].forEach((id) => {{
    const el = document.getElementById(id);
    if(el) el.innerHTML = "";
  }});
}}

function upDir(){{
  const d = document.getElementById("dir").value.trim();
  const parts = d.split("/").filter(Boolean);
  if(parts.length <= 1) return;
  parts.pop();
  document.getElementById("dir").value = "/" + parts.join("/");
  loadDir();
}}

async function doSearch(){{
  const d = document.getElementById("sroot").value.trim();
  const q = document.getElementById("sq").value.trim();
  const err = document.getElementById("sErr");
  const box = document.getElementById("sList");
  err.textContent = "Searching…";
  const res = await fetch(`/api/search?dir=${{encodeURIComponent(d)}}&q=${{encodeURIComponent(q)}}`);
  const data = await res.json();
  if(!res.ok){{ err.textContent = data.error || "Error"; box.innerHTML=""; return; }}
  err.textContent = `Found: ${{data.results.length}}`;
  _searchItems = data.results;
  box.innerHTML = data.results.map((it,i) => renderFileItem(it,i)).join("");
  updateBulkCount();
}}

const MB_FIELDS = ["musicbrainz_trackid","musicbrainz_albumid","musicbrainz_releasegroupid",
  "musicbrainz_artistid","musicbrainz_albumartistid","musicbrainz_releasecountry",
  "musicbrainz_releasestatus","musicbrainz_releasetype","musicbrainz_albumtype",
  "musicbrainz_albumstatus","musicbrainz_albumartist","musicbrainz_artist","musicbrainz_album",
  "barcode"];

const TAG_FIELDS = ["title","artist","album","albumartist","involved_people_list","date","genre",
  "year","original_year","track","publisher","comment","artist_sort","albumartist_sort",
  "catalog_number","bpm","art_url",...MB_FIELDS];

async function loadTags(pathOrSeq = "", seq = 0){{
  // Backward compatibility: loadTags(seq) and loadTags(path, seq) are both supported.
  let p = "";
  if(typeof pathOrSeq === "number"){{
    seq = pathOrSeq;
    p = document.getElementById("path").value.trim();
  }} else {{
    p = (pathOrSeq || "").trim() || document.getElementById("path").value.trim();
  }}
  const msg = document.getElementById("loadMsg");
  if(!p){{ msg.textContent = "No file selected. Double-click a file in Browse or Search to load."; return false; }}
  clearLookupResults();
  msg.textContent = "Loading\u2026";
  let res;
  let data;
  try {{
    res = await fetch(`/api/load?path=${{encodeURIComponent(p)}}`);
    data = await res.json();
  }} catch(err) {{
    msg.textContent = "Load failed (network/browser error).";
    logClickDebug("loadTags", "fetch failed", {{ path: p, error: String(err || "unknown") }});
    return false;
  }}
  if(seq && seq !== _activeLoad.seq && p !== _activeLoad.path) {{
    logClickDebug("loadTags", "Ignored stale response", {{ path: p, seq, activeSeq: _activeLoad.seq, activePath: _activeLoad.path }});
    return false;
  }}
  if(!res.ok){{ msg.textContent = data.error || "Error"; return false; }}
  for(const k of TAG_FIELDS){{
    if(data[k] !== undefined) setField(k, data[k]);
  }}
  // Autopopulate year from date (only if year is blank)
  const dv = getField("date"), yv = getField("year");
  if(dv && !yv) setField("year", dv.substring(0,4));
  // Autopopulate sort fields (only if blank)
  const artV = getField("artist"), artSortV = getField("artist_sort");
  if(artV && !artSortV) setField("artist_sort", sortName(artV));
  const aaSortV = getField("albumartist_sort"), aaV = getField("albumartist");
  if(aaV && !aaSortV) setField("albumartist_sort", sortName(aaV));
  // Set baseline from current field values (including autopopulated ones) so they don't appear dirty
  setBaseline(null);
  // Default missing track number to unsaved 1/1 so user reviews before save.
  if(!getField("track").trim()) setField("track", "1/1");
  const fn = p.split("/").pop();
  const cf = document.getElementById("currentFile");
  if(cf) cf.textContent = `Editing: ${{fn}} \u2014 ${{(data.length_seconds||0).toFixed(1)}}s | ${{data.bitrate_kbps||0}} kbps | ${{data.sample_rate_hz||0}} Hz`;
  msg.textContent = `Art: ${{data.has_art ? "\u2705 Yes" : "\u274c No"}}`;
  const artPrev = document.getElementById("artPreview");
  const artImg = document.getElementById("artImg");
  const artDims = document.getElementById("artDims");
  if(data.has_art){{
    const artTs = Date.now();
    artImg.src = `/api/art?path=${{encodeURIComponent(p)}}&full=1&t=${{artTs}}`;
    artPrev.style.display = "block";
    fetch(`/api/art_meta?path=${{encodeURIComponent(p)}}&t=${{artTs}}`).then(r=>r.json()).then(m=>{{
      if(m.width) artDims.textContent = `${{m.width}}\u00d7${{m.height}}`;
    }}).catch(()=>{{}});
  }} else {{
    artPrev.style.display = "none";
    artImg.src = "";
    if(artDims) artDims.textContent = "";
  }}
  // Always update web search query when a file is loaded
  const wsq = document.getElementById("wsq");
  if(wsq){{
    const art = data.artist || ""; const tit = data.title || "";
    const autoVal = (art && tit) ? `${{art}} \u2013 ${{tit}}` : (art || tit || fn.replace(/\\.mp3$/i,""));
    wsq.value = autoVal;
    _wsqAutoValue = autoVal;
  }}
  return true;
}}

function mbSearchDialog(){{
  const title = getField("title") || inferFromFilename();
  const artist = getField("artist");
  document.getElementById("mbDlgTitle").value = title;
  document.getElementById("mbDlgArtist").value = artist;
  document.getElementById("mbDlgAlbum").value = getField("album");
  document.getElementById("mbDlgYear").value = getField("year");
  document.getElementById("mbDlgResults").innerHTML = "";
  document.getElementById("mbDlgStatus").textContent = "";
  document.getElementById("mbDialog").style.display = "flex";
}}
function closeMbDialog(){{
  document.getElementById("mbDialog").style.display = "none";
}}
function inferFromFilename(){{
  const p = document.getElementById("path").value.trim();
  if(!p) return "";
  return p.split("/").pop().replace(/\\.mp3$/i,"");
}}
function runMbSearch(){{
  const title = document.getElementById("mbDlgTitle").value.trim();
  const artist = document.getElementById("mbDlgArtist").value.trim();
  const album = document.getElementById("mbDlgAlbum").value.trim();
  const year = document.getElementById("mbDlgYear").value.trim();
  if(!title && !artist){{ document.getElementById("mbDlgStatus").textContent = "Enter at least a title or artist."; return; }}
  document.getElementById("mbDlgStatus").textContent = "";
  document.getElementById("mbDlgResults").innerHTML = "";
  const params = new URLSearchParams();
  if(title) params.set("title",title);
  if(artist) params.set("artist",artist);
  if(album) params.set("album",album);
  if(year) params.set("year",year);
  startStream("mbSearch",
    `/api/mb_search_stream?${{params}}`,
    function(data) {{
      const el = document.getElementById("mbDlgResults");
      if(!data.results){{ document.getElementById("mbDlgStatus").textContent = data.error || "No results"; return; }}
      document.getElementById("mbDlgStatus").textContent = `${{data.results.length}} result(s)`;
      window._mb = data.results;
      el.innerHTML = data.results.map((r,i)=>{{
        const artUrl = r.cover_image || "";
        const thumbUrl = r.thumb || "";
        return `<div class="result-item" style="margin-top:8px">
          ${{thumbUrl ? ('<div style="float:right;margin-left:8px;text-align:center;line-height:1.2">'+'<img src="'+esc(thumbUrl)+'" style="max-height:52px;max-width:52px;border-radius:4px;object-fit:cover;display:block;cursor:pointer" onerror="this.parentNode.style.display=\\'none\\'" loading="lazy" onclick="showImageModal('+inlineJsString(artUrl||thumbUrl)+')" data-full="'+esc(artUrl||thumbUrl)+'" onload="onResultThumbLoad(this,this.dataset.full||this.src)">'+'<span style="font-size:.6rem;color:var(--muted)"></span></div>') : ""}}
          <strong>${{esc(r.title)}}</strong> — ${{esc(r.artist||"")}} <span style="color:var(--muted)">${{esc(r.date||"")}}</span>${{confidenceBadge(r.score)}}
          ${{r.album ? `<div class="hint">Album: <strong>${{esc(r.album)}}</strong>${{r.albumartist ? ` — ${{esc(r.albumartist)}}` : ""}}</div>` : ""}}
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:5px">
            <button type="button" class="btn btn-sm" onclick="applyMBFromDialog(${{i}})">Use</button>
            ${{r.id ? `<a href="https://musicbrainz.org/recording/${{esc(r.id)}}" target="_blank" rel="noopener" class="btn btn-sm btn-outline">Open ↗</a>` : ""}}
            ${{artUrl ? `<button type="button" class="btn btn-sm btn-outline" onclick='copyToClipboard(${{JSON.stringify(artUrl)}},this)' title="Copy full-size cover art URL to clipboard">&#128203; Art URL</button>` : ""}}
          </div>
          <div class="hint mono">MBID: ${{esc(r.id)}}</div>
        </div>`;
      }}).join("");
    }}
  );
}}
async function _autoFillGenreFromMB() {{
  if(getField("genre")) return;
  const recId = getField("musicbrainz_trackid");
  const relId = getField("musicbrainz_albumid");
  if(!recId && !relId) return;
  const params = new URLSearchParams();
  if(recId) params.set("recording_id", recId);
  if(relId) params.set("release_id", relId);
  try {{
    const res = await fetch(`/api/suggest_genre?${{params}}`);
    const data = await res.json();
    if(data.matches && data.matches[0] && data.matches[0].folder) {{
      setField("genre", data.matches[0].folder);
    }}
  }} catch(e) {{}}
}}

async function applyMBFromDialog(i){{
  const r = window._mb[i]; if(!r || !r.id) return;
  document.getElementById("mbDlgStatus").textContent = "Fetching full MB data\u2026";
  const res = await fetch(`/api/mb_recording?id=${{encodeURIComponent(r.id)}}`);
  const data = await res.json();
  if(!data.fields){{ document.getElementById("mbDlgStatus").textContent = data.error || "Error"; return; }}
  for(const [k,v] of Object.entries(data.fields)) setField(k,v);
  await _autoFillGenreFromMB();
  document.getElementById("mbDlgStatus").textContent = "\u2713 Applied all MB/Picard fields.";
  document.getElementById("mbResults").innerHTML = `<div class="result-item"><strong>${{esc(r.title||"")}}</strong> \u2014 ${{esc(r.artist||"")}} <span class="hint">\u2713 Applied from MusicBrainz</span></div>`;
  closeMbDialog();
}}

const _MAX_SSE_DATA = 3000;
const _streams = {{}};
function startStream(id, url, onResult) {{
  if (_streams[id]) {{ _streams[id].close(); delete _streams[id]; }}
  const panel = document.getElementById(id + "Status");
  if (!panel) return;
  panel.className = "stream-status active";
  panel.innerHTML = '<div class="log-line">Starting\u2026</div>';
  const es = new EventSource(url);
  _streams[id] = es;
  es.addEventListener("log", function(e) {{
    const d = document.createElement("div");
    d.className = "log-line";
    d.textContent = (e.data || "").slice(0, _MAX_SSE_DATA);
    panel.appendChild(d);
    panel.scrollTop = panel.scrollHeight;
  }});
  es.addEventListener("result", function(e) {{
    es.close(); delete _streams[id];
    panel.insertAdjacentHTML("beforeend", '<div class="s-ok">\u2713 Done</div>');
    try {{ onResult(JSON.parse(e.data)); }}
    catch(err) {{
      console.error("[SSE result] parse error:", err.message, "len=", e.data.length, "tail=", e.data.slice(-200));
      panel.insertAdjacentHTML("beforeend", `<div class="s-err">Parse error: ${{esc(err.message)}}</div>`);
    }}
  }});
  es.addEventListener("apierror", function(e) {{
    es.close(); delete _streams[id];
    panel.insertAdjacentHTML("beforeend", `<div class="s-err">\u2717 ${{esc((e.data||"").slice(0,_MAX_SSE_DATA))}}</div>`);
  }});
  es.onerror = function() {{
    if (_streams[id]) {{
      es.close(); delete _streams[id];
      panel.insertAdjacentHTML("beforeend", '<div class="s-err">\u2717 Connection lost</div>');
    }}
  }};
}}

function discogsSearch() {{
  const artist = getField("albumartist") || getField("artist");
  const album = getField("album");
  document.getElementById("discogsResults").innerHTML = "";
  document.getElementById("discogsTracklist").innerHTML = "";
  startStream("discogs",
    `/api/discogs_search_stream?artist=${{encodeURIComponent(artist)}}&album=${{encodeURIComponent(album)}}`,
    function(data) {{
      const el = document.getElementById("discogsResults");
      if (!data.results) {{ el.textContent = data.error || "No results"; return; }}
      window._dc = data.results;
      el.innerHTML = data.results.map((r,i) => `
        <div class="result-item">
          <strong>${{esc(r.title)}}</strong> <span style="color:var(--muted)">${{esc(r.year||"")}}</span>${{confidenceBadge(r.score)}}
          <div class="hint">Label: ${{esc(r.label||"")}} | Cat#: ${{esc(r.catno||"")}}</div>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:5px">
            <button type="button" class="btn btn-sm" onclick="discogsUse(${{i}})">Use + Tracklist</button>
            ${{r.uri ? `<a href="${{esc(r.uri)}}" target="_blank" rel="noopener" class="btn btn-sm btn-outline">Open \u2197</a>` : ""}}
            ${{(r.cover_image||r.thumb) ? `<button type="button" class="btn btn-sm btn-outline" onclick='copyToClipboard(${{JSON.stringify(r.cover_image||r.thumb)}},this)' title="Copy full-size cover art URL to clipboard">&#128203; Art URL</button>` : ""}}
            ${{r.thumb ? ('<div style="display:inline-block;text-align:center;line-height:1.2;vertical-align:middle">'+'<img src="'+esc(r.thumb)+'" style="max-height:50px;border-radius:6px;cursor:pointer;display:block" onclick="showImageModal('+inlineJsString(r.cover_image || r.thumb)+')" data-full="'+esc(r.cover_image || r.thumb)+'" title="Click to enlarge" onload="onResultThumbLoad(this,this.dataset.full||this.src)">'+'<span style="font-size:.6rem;color:var(--muted)"></span></div>') : ""}}
          </div>
        </div>`).join("");
    }}
  );
}}
function discogsUse(i) {{
  const r = window._dc[i]; if(!r || !r.id) return;
  document.getElementById("discogsTracklist").innerHTML = "";
  startStream("discogs",
    `/api/discogs_release_stream?id=${{encodeURIComponent(r.id)}}`,
    function(data) {{
      if (!data.fields) {{ document.getElementById("discogsResults").textContent = data.error || "Error"; return; }}
      for(const [k,v] of Object.entries(data.fields)) setField(k, v);
      document.getElementById("discogsResults").textContent = "Applied fields from Discogs. Pick a track below (optional).";
      checkArtUrlDim();
      const list = data.tracklist || [];
      const tl = document.getElementById("discogsTracklist");
      tl.innerHTML = list.length ? `<div class="result-item"><strong>Tracklist</strong><br>` +
        list.map(t => {{
          const pos = (t.position||"").trim();
          return `<div style="display:flex;gap:8px;padding:4px 0;cursor:pointer" onclick="applyTrack(\'${{esc(pos)}}\',${{JSON.stringify(t.title||"")}})">
            <span class="mono" style="min-width:28px">${{esc(pos||"")}}</span>
            <span style="flex:1">${{esc(t.title||"")}}</span>
            <span style="color:var(--muted)">${{esc(t.duration||"")}}</span>
          </div>`;
        }}).join("") + "</div>" : "";
    }}
  );
}}
function applyTrack(pos, title){{
  const m = (pos||"").match(/^\\d+$/);
  if(m) setField("track", pos);
  setField("title", title);
}}

async function parseUrl(){{
  const url = document.getElementById("purl").value.trim();
  const el = document.getElementById("parseResults");
  el.textContent = "Parsing…";
  const res = await fetch(`/api/parse_url?url=${{encodeURIComponent(url)}}`);
  const data = await res.json();
  if(!data.fields){{ el.textContent = data.error || "Error"; return; }}
  for(const [k,v] of Object.entries(data.fields)) setField(k, v);
  checkArtUrlDim();
  let noteHtml = esc(data.note || "Applied parsed fields (verify!)");
  if(data.fields.art_url) noteHtml += `<br><img src="${{esc(data.fields.art_url)}}" style="max-height:80px;border-radius:6px;margin-top:6px;cursor:pointer" onclick="showImageModal(this.src)" title="Click to enlarge">`;
  el.innerHTML = noteHtml;
}}

function webSearch(){{
  const wsq = document.getElementById("wsq");
  const q = (wsq ? wsq.value.trim() : "") || ((getField("artist")+" "+getField("title")).trim()) || inferFromFilename();
  if(!q){{ document.getElementById("webSearchResults").textContent = "Enter a search query or load a file first."; return; }}
  document.getElementById("webSearchResults").innerHTML = "";
  startStream("webSearch",
    `/api/web_search_stream?q=${{encodeURIComponent(q)}}&artist=${{encodeURIComponent(getField("artist"))}}&title=${{encodeURIComponent(getField("title"))}}&year=${{encodeURIComponent(getField("year"))}}&date=${{encodeURIComponent(getField("date"))}}`,
    function(data){{
      const el = document.getElementById("webSearchResults");
      if(!data.results_by_source){{ el.textContent = "No results found."; return; }}
      const sources = Object.keys(data.results_by_source);
      if(!sources.length){{ el.textContent = "No results found."; return; }}
      window._webResults = [];
      el.innerHTML = sources.map(source => {{
        const results = data.results_by_source[source] || [];
        const itemsHtml = results.map(r => {{
          const i = window._webResults.length;
          window._webResults.push(r);
          return `<div class="result-item">
            ${{r.thumb ? ('<div style="float:right;margin-left:8px;text-align:center;line-height:1.2">'+'<img src="'+esc(r.thumb)+'" style="max-height:52px;max-width:52px;border-radius:4px;object-fit:cover;display:block;cursor:pointer" onerror="this.parentNode.style.display=\\'none\\'" loading="lazy" onclick="showImageModal('+inlineJsString(r.cover_image || r.thumb)+')" data-full="'+esc(r.cover_image || r.thumb)+'" onload="onResultThumbLoad(this,this.dataset.full||this.src)">'+'<span style="font-size:.6rem;color:var(--muted)"></span></div>') : ""}}
            <strong>${{esc(r.title||"")}}</strong>${{r.artist ? ` \u2014 ${{esc(r.artist)}}` : ""}}
            ${{r.label ? `<div class="hint">Label: ${{esc(r.label)}}</div>` : ""}}
            ${{r.released ? `<div class="hint">Released: ${{esc(r.released)}}</div>` : ""}}
            ${{(r.track_number||r.bpm||r.genre||r.key) ? `<div class="hint">${{[r.track_number ? 'Track #: '+esc(String(r.track_number)) : '', r.genre ? 'Genre: '+esc(r.genre) : '', r.bpm ? 'BPM: '+esc(String(r.bpm)) : '', r.key ? 'Key: '+esc(r.key) : ''].filter(Boolean).join(' \u00b7 ')}}</div>` : ""}}
            <div style="display:flex;align-items:center;gap:8px;margin-top:5px;flex-wrap:wrap">
              ${{r.url ? `<a href="${{esc(r.url)}}" target="_blank" rel="noopener" class="btn btn-sm btn-outline">Open \u2197</a>` : ""}}
              ${{r.direct_url ? `<button type="button" class="btn btn-sm" onclick="applyWebResult(${{i}})">Parse URL</button>` : `<span style="color:var(--muted);font-size:.78rem;font-style:italic">Search page \u2014 open manually</span>`}}
              ${{(r.cover_image||r.thumb) ? `<button type="button" class="btn btn-sm btn-outline" onclick='copyToClipboard(${{JSON.stringify(r.cover_image||r.thumb)}},this)' title="Copy art URL to clipboard">&#128203; Art URL</button>` : ""}}
              ${{!r.is_fallback ? confidenceBadge(r.score) : ""}}
            </div>
            ${{r.note ? `<div class="hint">${{esc(r.note)}}</div>` : ""}}
          </div>`;
        }}).join("");
        return `<div style="margin-bottom:14px">
          <div style="font-weight:700;font-size:.9rem;margin-bottom:5px;padding:3px 8px;background:#f3f4f6;border-radius:4px">${{esc(source)}}</div>
          ${{itemsHtml || '<div class="hint" style="font-style:italic">No results.</div>'}}
        </div>`;
      }}).join("");
    }}
  );
}}
async function applyWebResult(i){{
  const r = window._webResults[i]; if(!r) return;
  if(!r.direct_url) {{
    if(r.url) window.open(r.url,"_blank","noopener");
    return;
  }}
  // Prefill BPM and genre from search result if those fields are currently blank
  if(r.bpm && !getField("bpm")) setField("bpm", String(r.bpm));
  if(r.genre && !getField("genre")) setField("genre", r.genre);
  if(r.track_number && !getField("track")) setField("track", String(r.track_number));
  document.getElementById("purl").value = r.url;
  parseUrl();
}}

function acoustid() {{
  const p = document.getElementById("path").value.trim();
  if(!p){{ document.getElementById("acoustidResults").textContent = "Load a file first."; return; }}
  document.getElementById("acoustidResults").innerHTML = "";
  startStream("acoustid",
    `/api/acoustid_stream?path=${{encodeURIComponent(p)}}`,
    function(data) {{
      const el = document.getElementById("acoustidResults");
      if (!data.results) {{ el.textContent = data.error || "No results"; return; }}
      window._ac = data.results;
      el.innerHTML = data.results.map((r,i) => `
        <div class="result-item">
          <strong>${{esc(r.title||"")}}</strong> — ${{esc(r.artist||"")}}
          ${{r.album ? `<div class="hint">Album: <strong>${{esc(r.album)}}</strong>${{r.albumartist ? ` — ${{esc(r.albumartist)}}` : ""}}${{r.year ? ` (${{esc(r.year)}})` : ""}}</div>` : ""}}
          <span style="color:var(--muted);font-size:.8rem">score: ${{(r.score||0).toFixed(3)}}</span>
          <button type="button" class="btn btn-sm" onclick="useAcoustID(${{i}})">Resolve via MB</button>
          <div class="hint mono">recording_id: ${{esc(r.recording_id||"")}}</div>
        </div>`).join("");
    }}
  );
}}
async function useAcoustID(i){{
  const r = window._ac[i]; if(!r || !r.recording_id) return;
  const st = document.getElementById("acoustidMbStatus");
  if(st) st.textContent = "Resolving via MB\u2026";
  const res = await fetch(`/api/mb_recording?id=${{encodeURIComponent(r.recording_id)}}`);
  const data = await res.json();
  if(!data.fields){{ if(st) st.textContent = data.error || "Error"; return; }}
  for(const [k,v] of Object.entries(data.fields)) setField(k, v);
  await _autoFillGenreFromMB();
  if(st) st.textContent = "\u2713 Applied all MB/Picard fields.";
}}

let _dirItems = [], _searchItems = [];
let _wsqAutoValue = "";
let _lastLoadRequest = {{ path: "", ts: 0 }};
let _activeLoad = {{ seq: 0, path: "" }};
let _listClickState = {{ dirList: {{ idx: -1, ts: 0 }}, sList: {{ idx: -1, ts: 0 }} }};
const CLICK_DEBUG_ENABLED = new URLSearchParams(window.location.search).get("clickDebug") === "1";

function logClickDebug(source, msg, extra = null) {{
  if(!CLICK_DEBUG_ENABLED) return;
  const ts = new Date().toISOString().slice(11, 23);
  const suffix = extra ? ` | ${{JSON.stringify(extra)}}` : "";
  const line = `[${{ts}}] [${{source}}] ${{msg}}${{suffix}}`;
  console.debug(line);
  const box = document.getElementById("clickDebug");
  if(!box) return;
  box.style.display = "block";
  box.textContent = `${{line}}\n${{box.textContent}}`.slice(0, 5000);
}}

window.addEventListener("error", (e) => {{
  const msg = `[window.error] ${{e.message}} @ ${{e.filename}}:${{e.lineno}}:${{e.colno}}`;
  console.error(msg, e.error);
  logClickDebug("window", msg);
}});

window.addEventListener("unhandledrejection", (e) => {{
  const msg = `[unhandledrejection] ${{String(e.reason || "unknown")}}`;
  console.error(msg);
  logClickDebug("window", msg);
}});

document.getElementById("wsq").addEventListener("input", function(){{ _wsqAutoValue = ""; }});

async function requestLoadFile(path, opts = {{}}) {{
  if(!path) return;
  const force = !!opts.force;
  const reason = opts.reason || "";
  const now = Date.now();
  if(!force && _lastLoadRequest.path === path && (now - _lastLoadRequest.ts) < 650) {{
    logClickDebug("requestLoadFile", "Skipped duplicate load request", {{ path, deltaMs: now - _lastLoadRequest.ts, reason }});
    return;
  }}
  _lastLoadRequest = {{ path, ts: now }};
  // Keep list selection/path state in sync before loading so a dblclick always
  // updates the right panel from the exact item the user activated.
  openFile(path);
  logClickDebug("requestLoadFile", "Loading file", {{ path, reason, force }});
  try {{
    await loadFileByPath(path);
  }} catch(err) {{
    logClickDebug("requestLoadFile", "Load failed", {{ path, reason, error: String(err || "unknown") }});
  }}
}}

function handleListItemActivation(listName, item) {{
  if(!item || !item.path) return;
  if(item.type === "dir") {{
    openDir(item.path);
    return;
  }}
  resetRightPaneSearchState();
  requestLoadFile(item.path, {{ force: true, reason: `${{listName}}-activate` }});
}}

function resetRightPaneSearchState() {{
  for (const [id, es] of Object.entries(_streams)) {{
    try {{ es.close(); }} catch (_) {{}}
    delete _streams[id];
  }}
  const statusIds = ["webSearchStatus", "acoustidStatus", "discogsStatus", "mbSearchStatus"];
  for (const id of statusIds) {{
    const el = document.getElementById(id);
    if(!el) continue;
    el.className = "stream-status";
    el.innerHTML = "";
  }}
  const resultIds = [
    "webSearchResults", "mbResults", "mbDlgResults", "acoustidResults",
    "discogsResults", "discogsTracklist", "parseResults", "acoustidMbStatus", "mbDlgStatus"
  ];
  for (const id of resultIds) {{
    const el = document.getElementById(id);
    if(el) el.innerHTML = "";
  }}
}}

function trackListClick(listName, idx, item) {{
  if(!item) return;
  if(item.path) openFile(item.path);
  const now = Date.now();
  const prev = _listClickState[listName] || {{ idx: -1, ts: 0 }};
  const isDouble = prev.idx === idx && (now - prev.ts) < 375;
  _listClickState[listName] = {{ idx, ts: now }};
  if(isDouble) {{
    logClickDebug(listName, "synthetic-double-activate", {{ idx, type: item?.type || null, path: item?.path || null }});
    handleListItemActivation(listName, item);
  }}
}}

document.getElementById("dirList").addEventListener("click", function(e){{
  if(e.target.closest(".file-item-select")) {{ updateBulkCount(); return; }}
  const row = e.target.closest("[data-idx]");
  if(!row) return;
  const idx = parseInt(row.dataset.idx, 10);
  const it = _dirItems[idx];
  trackListClick("dirList", idx, it);
}});
document.getElementById("dirList").addEventListener("dblclick", function(e){{
  if(e.target.closest(".file-item-select")) return;
  const item = e.target.closest("[data-idx]");
  if(!item) return;
  const idx = parseInt(item.dataset.idx, 10);
  const it = _dirItems[idx];
  logClickDebug("dirList", "dblclick", {{ idx, type: it?.type || null, path: it?.path || null }});
  handleListItemActivation("dirList", it);
}});

document.getElementById("sList").addEventListener("click", function(e){{
  if(e.target.closest(".file-item-select")) {{ updateBulkCount(); return; }}
  const row = e.target.closest("[data-idx]");
  if(!row) return;
  const idx = parseInt(row.dataset.idx, 10);
  const it = _searchItems[idx];
  trackListClick("sList", idx, it);
}});
document.getElementById("sList").addEventListener("dblclick", function(e){{
  if(e.target.closest(".file-item-select")) return;
  const item = e.target.closest("[data-idx]");
  if(!item) return;
  const idx = parseInt(item.dataset.idx, 10);
  const it = _searchItems[idx];
  logClickDebug("sList", "dblclick", {{ idx, path: it?.path || null }});
  handleListItemActivation("sList", it);
}});

document.getElementById("tagForm").addEventListener("submit", async function(e){{
  e.preventDefault();
  const fd = new FormData(e.target);
  if(e.submitter && e.submitter.name) fd.set(e.submitter.name, e.submitter.value);
  try {{
    const res = await fetch("/update", {{method:"POST", body:fd}});
    const data = await res.json();
    if(data.status === "ok") {{
      let html = `<div style="color:#065f46;font-size:1.1rem;font-weight:700;margin-bottom:12px">Tags written successfully</div>`;
      html += `<div><strong>Wrote to:</strong><br><span class="mono">${{esc(data.wrote_to)}}</span></div>`;
      if(data.archived_to) {{
        html += `<div style="margin-top:10px"><strong>Archived to:</strong><br><span class="mono">${{esc(data.archived_to)}}</span></div>`;
        // Update path input to the new archived location; browse directory panel is unchanged
        document.getElementById("path").value = data.archived_to;
      }}
      if(data.involved_people) html += `<div style="margin-top:8px"><strong>Involved people:</strong> ${{esc(data.involved_people)}}</div>`;
      if(data.label) html += `<div><strong>Label:</strong> ${{esc(data.label)}}</div>`;
      if(data.catalog_number) html += `<div><strong>Catalog #:</strong> ${{esc(data.catalog_number)}}</div>`;
      showModal(html);
      showToast("Tags saved successfully.", "success");
      // Reset dirty-field baseline to current saved state
      const savedSnap = {{}};
      for(const k of Object.keys(_baseline)) savedSnap[k] = getField(k);
      setBaseline(savedSnap);
      // Refresh art preview if art_url was set
      const artUrlVal = getField("art_url");
      if(artUrlVal){{
        const p2 = document.getElementById("path").value.trim();
        const artImg2 = document.getElementById("artImg");
        const artPrev2 = document.getElementById("artPreview");
        const artDims2 = document.getElementById("artDims");
        if(artImg2 && p2){{
          artImg2.onload = function() {{
            if(artDims2 && artImg2.naturalWidth && artImg2.naturalHeight) artDims2.textContent = artImg2.naturalWidth + "\u00d7" + artImg2.naturalHeight;
          }};
          artImg2.src = `/api/art?path=${{encodeURIComponent(p2)}}&full=1&t=${{Date.now()}}`;
          if(artPrev2) artPrev2.style.display = "block";
        }}
      }}
      // Refresh browse/search lists so saved tags are immediately visible.
      await loadDir();
      await doSearch();
    }} else {{
      showModal(`<div style="color:#991b1b;font-size:1.1rem;font-weight:700;margin-bottom:12px">Error</div><div>${{esc(data.error || "Unknown error")}}</div>`);
      showToast(data.error || "Save failed", "error");
    }}
  }} catch(err) {{
    showModal(`<div style="color:#991b1b">Network error: ${{esc(err.message)}}</div>`);
    showToast("Network error while saving.", "error");
  }}
}});

function showModal(html) {{
  document.getElementById("resultModalBody").innerHTML = html;
  document.getElementById("resultModal").classList.add("open");
}}

function showArtModal(src) {{
  const imgId = "artModalImg_" + Date.now();
  const dimId = "artModalDim_" + Date.now();
  showModal(`
    <img id="${{imgId}}" src="${{esc(src)}}" style="max-width:100%;border-radius:8px;display:block" alt="Cover art"/>
    <div id="${{dimId}}" style="color:var(--muted);font-size:.82rem;margin-top:6px;text-align:center"></div>
  `);
  const img = document.getElementById(imgId);
  if(img) {{
    img.addEventListener("load", function() {{
      const d = document.getElementById(dimId);
      if(d && img.naturalWidth && img.naturalHeight) d.textContent = img.naturalWidth + "\u00d7" + img.naturalHeight;
    }});
  }}
}}

const _coverDimCache = new Map();

function onResultThumbLoad(img, fullSrc) {{
  const d = img ? img.nextElementSibling : null;
  const full = String(fullSrc || "").trim();
  if(!d || !/^https?:\/\//i.test(full)) return;
  const cached = _coverDimCache.get(full);
  if(cached) {{
    d.textContent = cached.width + "×" + cached.height;
    return;
  }}
  fetch(`/api/url_dim?url=${{encodeURIComponent(full)}}`)
    .then(r => r.json())
    .then(data => {{
      if(!data || !data.width || !data.height) return;
      _coverDimCache.set(full, {{ width: data.width, height: data.height }});
      d.textContent = data.width + "×" + data.height;
    }})
    .catch(() => {{}});
}}

function showImageModal(src) {{
  const s = String(src || "");
  if(!/^https?:\\/\\//i.test(s)) return;
  const imgId = "imgModalImg_" + Date.now();
  const dimId = "imgModalDim_" + Date.now();
  showModal(`
    <img id="${{imgId}}" src="${{esc(s)}}" style="max-width:100%;border-radius:8px;display:block" alt=""/>
    <div id="${{dimId}}" style="color:var(--muted);font-size:.82rem;margin-top:6px;text-align:center"></div>
  `);
  const img = document.getElementById(imgId);
  if(img) {{
    img.addEventListener("load", function() {{
      const d = document.getElementById(dimId);
      if(d && img.naturalWidth && img.naturalHeight) d.textContent = img.naturalWidth + "\u00d7" + img.naturalHeight;
    }});
  }}
}}

async function loadKeyStatus(){{
  try{{
    const res = await fetch("/api/key_status");
    if(!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    const dk = document.getElementById("discogsKeyStatus");
    const ak = document.getElementById("acoustidKeyStatus");
    if(dk) dk.textContent = data.discogs?.display || "DISCOGS_TOKEN not set";
    if(ak) ak.textContent = data.acoustid?.display || "ACOUSTID_KEY not set";
  }} catch(e){{
    const dk = document.getElementById("discogsKeyStatus");
    const ak = document.getElementById("acoustidKeyStatus");
    const msg = "key status unavailable: " + e.message;
    if(dk) dk.textContent = msg;
    if(ak) ak.textContent = msg;
  }}
}}

document.addEventListener("keydown", function(e){{
  if(e.key === "Escape") {{
    const mbDlg = document.getElementById("mbDialog");
    if(mbDlg && mbDlg.style.display !== "none") closeMbDialog();
    else closeModal();
  }}
}});

function copyToClipboard(text, btn) {{
  const prev = btn ? btn.textContent : "";
  function onSuccess() {{
    if(btn) {{ btn.textContent = "\u2713 Copied!"; setTimeout(()=>{{ btn.textContent = prev; }}, 1500); }}
  }}
  function onFail(err) {{
    if(btn) {{ btn.textContent = "\u274c Failed"; setTimeout(()=>{{ btn.textContent = prev; }}, 2000); }}
    console.warn("copyToClipboard:", err);
  }}
  if(navigator.clipboard && window.isSecureContext) {{
    navigator.clipboard.writeText(text).then(onSuccess, onFail);
  }} else {{
    // Fallback for non-secure contexts or older browsers
    try {{
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;top:-9999px;left:-9999px;opacity:0";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if(ok) onSuccess(); else onFail(new Error("execCommand returned false"));
    }} catch(e) {{ onFail(e); }}
  }}
}}

async function revertTags() {{
  const p = document.getElementById("path").value.trim();
  if(!p) return;
  const res = await fetch(`/api/load?path=${{encodeURIComponent(p)}}`);
  const data = await res.json();
  if(!res.ok) return;
  for(const k of TAG_FIELDS){{
    if(data[k] !== undefined) setField(k, data[k]);
  }}
  // Autopopulate year from date (only if year is blank)
  const dv = getField("date"), yv = getField("year");
  if(dv && !yv) setField("year", dv.substring(0,4));
  // Autopopulate sort fields (only if blank)
  const artV = getField("artist"), artSortV = getField("artist_sort");
  if(artV && !artSortV) setField("artist_sort", sortName(artV));
  const aaSortV = getField("albumartist_sort"), aaV = getField("albumartist");
  if(aaV && !aaSortV) setField("albumartist_sort", sortName(aaV));
  setBaseline(null);
  clearLookupResults();
}}

async function checkArtUrlDim() {{
  const v = document.getElementById("art_url_field");
  const d = document.getElementById("artUrlDims");
  if(!v || !v.value.trim()) {{ if(d) d.textContent = ""; return; }}
  if(d) d.textContent = "Checking\u2026";
  try {{
    const res = await fetch(`/api/url_dim?url=${{encodeURIComponent(v.value.trim())}}`);
    const data = await res.json();
    if(d) d.textContent = data.width ? `${{data.width}}\u00d7${{data.height}}` : (data.error ? "" : "");
  }} catch(e) {{ if(d) d.textContent = ""; }}
}}

function closeModal() {{
  document.getElementById("resultModal").classList.remove("open");
}}

// Real-time dirty tracking for manual input changes
document.getElementById("tagForm").addEventListener("input", function(e) {{
  const name = e.target.name;
  if(!name || !(name in _baseline)) return;
  if(e.target.value !== (_baseline[name] || "")) e.target.classList.add("dirty");
  else e.target.classList.remove("dirty");
  setInlineFieldError(name, "");
  updateRevertUI(name);
}});

// Delegated click handler for per-field revert buttons
document.getElementById("tagForm").addEventListener("click", function(e) {{
  const btn = e.target.closest(".revert-btn");
  if(!btn) return;
  const name = btn.dataset.revertFor;
  if(!name || !(name in _baseline)) return;
  setField(name, _baseline[name]);
}});

async function suggestGenre() {{
  const rec_id = getField("musicbrainz_trackid");
  const rel_id = getField("musicbrainz_albumid");
  const el = document.getElementById("genreSuggestions");
  if(!rec_id && !rel_id) {{
    el.innerHTML = '<span style="color:var(--muted);font-size:.82rem">Load MusicBrainz data first (need Track ID or Album ID).</span>';
    return;
  }}
  el.innerHTML = '<span style="color:var(--muted);font-size:.82rem">Fetching MusicBrainz tags\u2026</span>';
  const params = new URLSearchParams();
  if(rec_id) params.set("recording_id", rec_id);
  if(rel_id) params.set("release_id", rel_id);
  try {{
    const res = await fetch(`/api/suggest_genre?${{params}}`);
    const data = await res.json();
    if(data.error) {{ el.innerHTML = `<span style="color:#991b1b;font-size:.82rem">${{esc(data.error)}}</span>`; return; }}
    let html = "";
    if(data.matches && data.matches.length) {{
      html += '<div style="font-size:.82rem;font-weight:600;margin-bottom:4px">Folder matches:</div>';
      html += data.matches.slice(0,5).map(m =>
        `<button type="button" class="btn btn-sm ${{m.exact ? '' : 'btn-outline'}}"
          onclick="applyGenreSuggestion(${{JSON.stringify(m.folder)}})"
          title="MB tag: ${{esc(m.mb_tag)}} | score: ${{m.score}}"
          style="margin-bottom:4px">
          ${{esc(m.folder)}}${{m.exact ? ' \u2713' : ` (${{Math.round(m.score*100)}}%)`}}
        </button>`
      ).join("");
    }}
    if(data.mb_tags && data.mb_tags.length) {{
      html += '<div style="font-size:.82rem;font-weight:600;margin-top:6px;margin-bottom:4px">MB tags:</div>';
      html += data.mb_tags.slice(0,8).map(t =>
        `<button type="button" class="btn btn-sm btn-ghost"
          onclick="applyGenreSuggestion(${{JSON.stringify(t.name)}})"
          title="count: ${{t.count}}"
          style="margin-bottom:4px">
          ${{esc(t.name)}}${{t.count > 1 ? ` (${{t.count}})` : ""}}
        </button>`
      ).join("");
    }}
    if(!html) html = '<span style="color:var(--muted);font-size:.82rem">No MusicBrainz tags found for this recording.</span>';
    el.innerHTML = html;
  }} catch(err) {{
    el.innerHTML = `<span style="color:#991b1b;font-size:.82rem">Error: ${{esc(err.message)}}</span>`;
  }}
}}
function applyGenreSuggestion(genre) {{
  setField("genre", genre);
  document.getElementById("genreSuggestions").innerHTML =
    `<span style="color:#065f46;font-size:.82rem">\u2713 Applied: ${{esc(genre)}}</span>`;
}}

(async function init(){{
  logClickDebug("init", "Starting init");
  try {{
    const pingRes = await fetch("/api/ping");
    const pingData = await pingRes.json();
    logClickDebug("init", "ping", pingData);
  }} catch(e) {{
    logClickDebug("init", "ping failed", {{ error: String(e) }});
  }}
  try {{
    await loadDir();
    logClickDebug("init", "loadDir ok");
  }} catch(e) {{
    logClickDebug("init", "loadDir failed", {{ error: String(e) }});
  }}
  try {{
    await loadKeyStatus();
    logClickDebug("init", "loadKeyStatus ok");
  }} catch(e) {{
    logClickDebug("init", "loadKeyStatus failed", {{ error: String(e) }});
  }}
}})();
</script>
</body>
</html>"""


@app.route("/update", methods=["POST"])
def update():
    if not basic_auth_ok():
        return require_basic_auth()
    try:
        path = safe_path(request.form.get("path", ""))

        fields = {k: request.form.get(k, "") for k in [
            "title","artist","album","albumartist","involved_people_list",
            "date","genre","year","original_year","track","publisher","comment",
            "artist_sort","albumartist_sort","art_url","catalog_number",
            "bpm",
            *MB_TXXX_FIELDS,
        ]}

        if not fields["artist_sort"].strip():
            fields["artist_sort"] = sort_name(fields.get("artist",""))
        if not fields["albumartist_sort"].strip():
            fields["albumartist_sort"] = sort_name(fields.get("albumartist",""))

        upsert_id3(path, fields)

        action = request.form.get("action", "write")
        archived_to = ""
        if action == "archive":
            archived_to = archive_mp3(path)

        return jsonify({
            "status": "ok",
            "wrote_to": path,
            "archived_to": archived_to,
            "involved_people": normalize_involved_people(fields.get('involved_people_list', '')),
            "catalog_number": fields.get('catalog_number', ''),
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400

if __name__ == "__main__":

    app.run(host="0.0.0.0", port=5010)
