import os
import re
import base64
import json
import shutil
from io import BytesIO
from urllib.parse import urlparse, parse_qs, unquote

import requests
import acoustid
from bs4 import BeautifulSoup
from flask import Flask, request, Response, jsonify
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TALB, TPE2, TCON, TRCK, TPUB, COMM, TDRC, TYER,
    TXXX, TSOP, TSO2, APIC
)
from mutagen.mp3 import MP3
from PIL import Image

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
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "").strip()

UA = "CorsicanEscapeTagEditor/2.0 (nick@corsicanescape.com)"

# MusicBrainz Picard-standard TXXX field names
MB_TXXX_FIELDS = [
    "musicbrainz_trackid",
    "musicbrainz_albumid",
    "musicbrainz_releasegroupid",
    "musicbrainz_artistid",
    "musicbrainz_albumartistid",
    "musicbrainz_releasetrackid",
    "musicbrainz_workid",
    "musicbrainz_trmid",
    "musicbrainz_discid",
    "musicbrainz_releasecountry",
    "musicbrainz_releasestatus",
    "musicbrainz_releasetype",
    "musicbrainz_albumtype",
    "musicbrainz_albumstatus",
    "musicbrainz_albumartist",
    "musicbrainz_artist",
    "musicbrainz_album",
    "barcode",
    "asin",
]

# Lightweight tag cache: (path, mtime) -> dict (max 2000 entries)
_tag_cache: dict = {}
_TAG_CACHE_MAX = 2000

app = Flask(__name__)

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

    involved = normalize_involved_people(fields.get("involved_people_list") or "")
    set_txxx(tags, "involved_people_list", involved)

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

    # extra fields
    set_txxx(tags, "label", fields.get("label", ""))
    set_txxx(tags, "catalog_number", fields.get("catalog_number", ""))

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
        "original_year": get_txxx(tags, "original_year"),
        "artist_sort": get_text(tags, "TSOP"),
        "albumartist_sort": get_text(tags, "TSO2"),
        "involved_people_list": get_txxx(tags, "involved_people_list"),
        "label": get_txxx(tags, "label"),
        "catalog_number": get_txxx(tags, "catalog_number"),
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
        tags = ID3(path)
        pics = tags.getall("APIC")
        if not pics:
            return Response(status=404)
        data = pics[0].data
        im = Image.open(BytesIO(data)).convert("RGB")
        im.thumbnail((80, 80), Image.LANCZOS)
        out = BytesIO()
        im.save(out, format="JPEG", quality=75)
        return Response(out.getvalue(), mimetype="image/jpeg",
                        headers={"Cache-Control": "max-age=3600"})
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
    if not title:
        return jsonify({"error": "Provide at least a title."}), 400

    q = f'recording:"{title}"'
    if artist:
        q += f' AND artist:"{artist}"'
    if album:
        q += f' AND release:"{album}"'

    r = requests.get("https://musicbrainz.org/ws/2/recording",
                     params={"query": q, "fmt": "json", "limit": 10},
                     timeout=25, headers=mb_headers())
    r.raise_for_status()
    data = r.json()
    out = []
    for rec in data.get("recordings", []):
        ac = rec.get("artist-credit", [])
        artist_name = ac[0].get("name") if ac else ""
        out.append({
            "id": rec.get("id", ""),
            "title": rec.get("title", ""),
            "artist": artist_name or "",
            "date": rec.get("first-release-date", "") or "",
        })
    return jsonify({"results": out})

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

    r = requests.get(f"https://musicbrainz.org/ws/2/{entity}/{mbid}",
                     params={"fmt": "json", "inc": "artist-credits+releases"},
                     timeout=25, headers=mb_headers())
    r.raise_for_status()
    data = r.json()

    if entity == "recording":
        ac = data.get("artist-credit", [])
        artist = ac[0].get("name") if ac else ""
        date = data.get("first-release-date", "") or ""
        return jsonify({"fields": {
            "title": data.get("title", "") or "",
            "artist": artist or "",
            "date": date,
            "year": date[:4] if date[:4].isdigit() else ""
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

    host = urlparse(url).netloc.lower()
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

@app.route("/api/beatport_search", methods=["GET"])
def api_beatport_search():
    if not basic_auth_ok():
        return require_basic_auth()
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Provide a query."}), 400
    url = f"https://www.beatport.com/search?q={requests.utils.quote(q)}"
    return jsonify({"open_url": url, "note": "Beatport search is dynamic. Open link and paste a specific track/release URL into Parse URL."})

# ----- AcoustID -> MB recording resolve -----
import tempfile
import subprocess

@app.route("/api/acoustid", methods=["GET"])
def api_acoustid():
    if not basic_auth_ok():
        return require_basic_auth()
    if not ACOUSTID_KEY:
        return jsonify({"error": "ACOUSTID_KEY not set."}), 400

    path = safe_path(request.args.get("path", ""))

    def do_match(pth: str):
        return acoustid.match(ACOUSTID_KEY, pth, parse=True)

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

    out = []
    for score, recording_id, title, artist in results[:10]:
        out.append({
            "score": float(score),
            "recording_id": recording_id or "",
            "title": title or "",
            "artist": artist or "",
        })
    return jsonify({"results": out})

# ----- Last.fm genre suggest -----
@app.route("/api/lastfm_genre", methods=["GET"])
def api_lastfm_genre():
    if not basic_auth_ok():
        return require_basic_auth()
    if not LASTFM_API_KEY:
        return jsonify({"error": "LASTFM_API_KEY not set."}), 400

    artist = (request.args.get("artist") or "").strip()
    title = (request.args.get("title") or "").strip()
    if not artist or not title:
        return jsonify({"error": "Provide artist and title."}), 400

    r = http_get("https://ws.audioscrobbler.com/2.0/",
                 params={"method":"track.getTopTags","api_key":LASTFM_API_KEY,"artist":artist,"track":title,"format":"json"},
                 timeout=25)
    r.raise_for_status()
    data = r.json()
    tags = data.get("toptags", {}).get("tag", []) or []
    top = []
    for t in tags[:12]:
        name = t.get("name", "")
        if name:
            top.append(name)
    return jsonify({"results": top})

# ---------------- UI ----------------
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
  <style>
    :root {{
      --accent: #3b6fd4;
      --border: #d1d5db;
      --bg: #f9fafb;
      --card-bg: #ffffff;
      --thumb-size: 44px;
      --text: #111827;
      --muted: #6b7280;
      --min-bg: #fffbeb;
      --min-border: #f59e0b;
      --no-genre: #fff0f0;
      --radius: 10px;
      --shadow: 0 1px 4px rgba(0,0,0,.08);
    }}
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: system-ui, "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 16px 20px;
      max-width: 1300px;
    }}
    h1 {{ font-size: 1.5rem; font-weight: 700; margin: 0 0 4px; }}
    h2 {{ font-size: 1.1rem; font-weight: 600; margin: 0 0 10px; color: var(--accent); }}
    h3 {{ font-size: .95rem; font-weight: 600; margin: 0 0 8px; }}
    p.sub {{ color: var(--muted); font-size: .85rem; margin: 0 0 16px; }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px;
      margin-bottom: 16px;
      box-shadow: var(--shadow);
    }}
    .row2 {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }}
    .row2 > * {{ min-width: 0; }}
    @media(max-width:700px){{ .row2 {{ grid-template-columns: 1fr; }} }}
    label {{ display: block; font-size: .85rem; font-weight: 600; margin-bottom: 4px; }}
    input[type=text], input:not([type]), textarea {{
      width: 100%; padding: 8px 10px;
      border: 1px solid var(--border); border-radius: 6px;
      font-size: .9rem; background: #fff; color: var(--text);
      margin-bottom: 12px; transition: border-color .15s;
    }}
    input:focus, textarea:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(59,111,212,.15); }}
    textarea {{ resize: vertical; }}
    .hint {{ color: var(--muted); font-size: .78rem; margin: -8px 0 10px; }}
    .btn {{
      display: inline-block; padding: 8px 14px; font-size: .85rem; font-weight: 600;
      border: none; border-radius: 6px; background: var(--accent); color: #fff;
      cursor: pointer; margin-right: 6px; margin-bottom: 6px; transition: opacity .15s;
    }}
    .btn:hover {{ opacity: .88; }}
    .btn-sm {{ padding: 5px 10px; font-size: .8rem; }}
    .btn-outline {{ background: transparent; border: 1px solid var(--accent); color: var(--accent); }}
    .btn-ghost {{ background: #f3f4f6; color: var(--text); border: 1px solid var(--border); }}
    .mono {{ font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; font-size: .82rem; }}
    .callout-min {{
      border: 2px solid var(--min-border); background: var(--min-bg);
      border-radius: var(--radius); padding: 14px 16px; margin-bottom: 16px;
    }}
    .callout-min .callout-title {{
      font-size: .78rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: .06em; color: #b45309; margin-bottom: 12px;
    }}
    .file-list {{
      max-height: 380px; overflow-y: auto;
      border: 1px solid var(--border); border-radius: var(--radius); background: var(--card-bg);
    }}
    .file-item {{
      display: flex; align-items: flex-start; gap: 10px; padding: 8px 10px;
      cursor: pointer; border-bottom: 1px solid #f0f0f0; transition: background .1s;
    }}
    .file-item:last-child {{ border-bottom: none; }}
    .file-item:hover {{ background: #f0f4ff; }}
    .file-item.no-genre {{ background: var(--no-genre); }}
    .file-item.no-genre:hover {{ background: #ffe4e4; }}
    .file-thumb {{ width: var(--thumb-size); height: var(--thumb-size); border-radius: 6px; object-fit: cover; flex-shrink: 0; }}
    .file-thumb-placeholder {{
      width: var(--thumb-size); height: var(--thumb-size); border-radius: 6px; background: #e5e7eb;
      flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 1.3rem;
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
    .dir-item:hover {{ background: #f0f4ff; }}
    details.mb-section summary {{
      cursor: pointer; font-size: .95rem; font-weight: 600; color: var(--accent);
      padding: 8px 0; user-select: none; list-style: disclosure-closed;
    }}
    details.mb-section[open] summary {{ margin-bottom: 12px; list-style: disclosure-open; }}
    .result-item {{ border: 1px solid var(--border); border-radius: 8px; padding: 10px; margin-bottom: 8px; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .section-sep {{ border: none; border-top: 1px solid var(--border); margin: 16px 0; }}
    .field-group {{ margin-bottom: 0; }}
    #resultModal {{
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5);
      z-index: 1000; align-items: center; justify-content: center;
    }}
    #resultModal.open {{ display: flex; }}
    .modal-inner {{
      background: var(--card-bg); border-radius: var(--radius); padding: 24px;
      max-width: 540px; width: 90%; box-shadow: 0 8px 32px rgba(0,0,0,.25);
    }}
  </style>
</head>
<body>

<h1>&#127925; MP3 Tag Editor</h1>
<p class="sub">Music root: <span class="mono">{MUSIC_ROOT}</span></p>

<div class="row2">
  <div class="card">
    <h2>Browse</h2>
    <p class="sub">Navigate folders and click a file to edit.</p>
    <label>Directory</label>
    <input id="dir" value="{browse_default}"/>
    <label>Filter (optional)</label>
    <input id="dirFilter" placeholder="e.g. maribou or 2024"/>
    <button class="btn" type="button" onclick="loadDir()">Load</button>
    <button class="btn btn-ghost" type="button" onclick="upDir()">&#8593; Up</button>
    <div id="dirErr" class="hint"></div>
    <div class="file-list" id="dirList"></div>
  </div>

  <div class="card">
    <h2>Search</h2>
    <p class="sub">Search recursively under a directory.</p>
    <label>Search root</label>
    <input id="sroot" value="{MUSIC_ROOT}"/>
    <label>Query</label>
    <input id="sq" placeholder="filename or partial path"/>
    <button class="btn" type="button" onclick="doSearch()">Search</button>
    <div id="sErr" class="hint"></div>
    <div class="file-list" id="sList"></div>
  </div>
</div>

<div class="card">
  <h2>Edit Tags</h2>
  <p class="sub">Load a file, optionally use lookups, then write. Archive reorganises to <span class="mono">{MUSIC_ROOT}/Genre/AlbumArtist/Album [Year]/</span>.</p>

  <form id="tagForm" method="POST" action="/update">
    <label>File path</label>
    <input name="path" id="path" value="{path}"/>
    <button type="button" class="btn" onclick="loadTags()">Load existing tags &amp; audio info</button>
    <div id="loadMsg" class="hint"></div>

    <hr class="section-sep"/>

    <div class="card" style="background:var(--bg)">
      <h3>Lookups</h3>
      <div class="row2">
        <div>
          <button type="button" class="btn btn-outline" onclick="mbSearch()">MusicBrainz Search</button>
          <input id="mbref" placeholder="Paste MusicBrainz URL or MBID"/>
          <button type="button" class="btn btn-outline" onclick="mbResolve()">Resolve MB URL/MBID</button>
          <div id="mbResults"></div>
        </div>
        <div>
          <button type="button" class="btn btn-outline" onclick="discogsSearch()">Discogs Search (album)</button>
          <p class="sub">Needs DISCOGS_TOKEN env. Includes tracklist picker.</p>
          <div id="discogsResults"></div>
          <div id="discogsTracklist"></div>
        </div>
      </div>
      <div class="row2">
        <div>
          <input id="purl" placeholder="Paste URL: Beatport / Bandcamp / Juno / Traxsource"/>
          <button type="button" class="btn btn-outline" onclick="parseUrl()">Parse URL</button>
          <input id="bpq" placeholder="Beatport search query (optional)"/>
          <button type="button" class="btn btn-outline" onclick="beatportSearch()">Beatport Search</button>
          <div id="parseResults"></div>
        </div>
        <div>
          <button type="button" class="btn btn-outline" onclick="acoustid()">AcoustID Fingerprint</button>
          <p class="sub">Needs ACOUSTID_KEY env.</p>
          <div id="acoustidResults"></div>
          <button type="button" class="btn btn-outline" onclick="lastfm()">Last.fm Genre Suggest</button>
          <p class="sub">Needs LASTFM_API_KEY env.</p>
          <div id="lastfmResults"></div>
        </div>
      </div>
    </div>

    <hr class="section-sep"/>

    <div class="callout-min">
      <div class="callout-title">&#11088; Minimum required tags</div>
      <div class="row2">
        <div>
          <div class="field-group"><label>Title</label><input name="title"/></div>
          <div class="field-group"><label>Artist</label><input name="artist"/></div>
          <div class="field-group"><label>Album</label><input name="album"/></div>
          <div class="field-group"><label>Album Artist (band)</label><input name="albumartist"/></div>
          <div class="field-group">
            <label>Involved people list</label>
            <input name="involved_people_list" placeholder="Britney Spears, Christina Aguilera"/>
            <div class="hint">Stored as <span class="mono">TXXX:involved_people_list</span></div>
          </div>
        </div>
        <div>
          <div class="field-group"><label>Date</label><input name="date" placeholder="YYYY or YYYY-MM-DD"/></div>
          <div class="field-group"><label>Year</label><input name="year" placeholder="YYYY"/></div>
          <div class="field-group"><label>Original Year</label><input name="original_year" placeholder="YYYY"/></div>
          <div class="field-group"><label>Genre</label><input name="genre"/></div>
          <div class="field-group"><label>Track number</label><input name="track" placeholder="1 or 1/12"/></div>
          <div class="field-group"><label>Publisher</label><input name="publisher"/></div>
          <div class="field-group"><label>Comment</label><textarea name="comment" rows="3"></textarea></div>
        </div>
      </div>
      <div class="row2">
        <div>
          <div class="field-group">
            <label>Artist sort</label><input name="artist_sort" placeholder="Beatles, The"/>
            <div class="hint">Auto-generates for names starting with &ldquo;The &hellip;&rdquo;</div>
            <label style="font-weight:400;font-size:.82rem"><input type="checkbox" name="auto_artist_sort" value="1" checked/> Auto-generate if blank</label>
          </div>
        </div>
        <div>
          <div class="field-group">
            <label>Album artist sort</label><input name="albumartist_sort" placeholder="Beatles, The"/>
            <div class="hint">Auto-generates for names starting with &ldquo;The &hellip;&rdquo;</div>
            <label style="font-weight:400;font-size:.82rem"><input type="checkbox" name="auto_albumartist_sort" value="1" checked/> Auto-generate if blank</label>
          </div>
        </div>
      </div>
      <div class="row2">
        <div>
          <div class="field-group">
            <label>Label</label><input name="label" placeholder="(optional)"/>
            <div class="hint">Stored as <span class="mono">TXXX:label</span></div>
          </div>
        </div>
        <div>
          <div class="field-group">
            <label>Catalog number</label><input name="catalog_number" placeholder="(optional)"/>
            <div class="hint">Stored as <span class="mono">TXXX:catalog_number</span></div>
          </div>
        </div>
      </div>
      <div class="field-group">
        <label>Cover art image URL</label>
        <input name="art_url" placeholder="https://.../cover.jpg (embedded as JPEG)"/>
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
          <div class="field-group"><label>MusicBrainz Release Track ID</label><input name="musicbrainz_releasetrackid" class="mono" placeholder="UUID"/></div>
          <div class="field-group"><label>MusicBrainz Work ID</label><input name="musicbrainz_workid" class="mono" placeholder="UUID"/></div>
          <div class="field-group"><label>MusicBrainz TRMID (legacy)</label><input name="musicbrainz_trmid" class="mono" placeholder="UUID"/></div>
          <div class="field-group"><label>MusicBrainz Disc ID</label><input name="musicbrainz_discid" class="mono" placeholder="disc id"/></div>
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
          <div class="field-group"><label>ASIN</label><input name="asin" placeholder="Amazon ASIN"/></div>
        </div>
      </div>
    </details>

    <button class="btn" type="submit" name="action" value="write">&#128190; Write tags</button>
    <button class="btn btn-ghost" type="submit" name="action" value="archive">&#128230; Write tags + Archive</button>
  </form>
</div>

<div id="resultModal">
  <div class="modal-inner">
    <div id="resultModalBody"></div>
    <button type="button" class="btn" style="margin-top:16px" onclick="closeModal()">Close</button>
  </div>
</div>

<script>
function esc(s){{ return (s||"").toString().replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;"); }}
function setField(name, val){{ const el = document.querySelector(`[name="${{name}}"]`); if(el) el.value = val || ""; }}
function getField(name){{ const el = document.querySelector(`[name="${{name}}"]`); return el ? el.value : ""; }}

function renderFileItem(it, idx){{
  const noGenre = it.type === "file" && !it.genre;
  const genreBadge = it.type === "file"
    ? (it.genre ? `<span class="genre-badge">${{esc(it.genre)}}</span>` : `<span class="genre-missing">no genre</span>`)
    : "";
  if(it.type === "dir"){{
    return `<div class="dir-item" data-idx="${{idx}}">
      <div class="file-thumb-placeholder">&#128193;</div><span>${{esc(it.name)}}</span>
    </div>`;
  }}
  const thumb = it.has_art
    ? `<img class="file-thumb" src="/api/art?path=${{encodeURIComponent(it.path)}}" alt="" loading="lazy" onerror="this.outerHTML='<div class=file-thumb-placeholder>&#127925;</div>'">`
    : `<div class="file-thumb-placeholder">&#127925;</div>`;
  return `<div class="file-item${{noGenre ? ' no-genre' : ''}}" data-idx="${{idx}}">
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
}}

function openDir(p){{ document.getElementById("dir").value = p; loadDir(); }}
function openFile(p){{ document.getElementById("path").value = p; document.getElementById("path").scrollIntoView({{behavior:"smooth"}}); }}

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
}}

const MB_FIELDS = ["musicbrainz_trackid","musicbrainz_albumid","musicbrainz_releasegroupid",
  "musicbrainz_artistid","musicbrainz_albumartistid","musicbrainz_releasetrackid",
  "musicbrainz_workid","musicbrainz_trmid","musicbrainz_discid","musicbrainz_releasecountry",
  "musicbrainz_releasestatus","musicbrainz_releasetype","musicbrainz_albumtype",
  "musicbrainz_albumstatus","musicbrainz_albumartist","musicbrainz_artist","musicbrainz_album",
  "barcode","asin"];

async function loadTags(){{
  const p = document.getElementById("path").value.trim();
  const msg = document.getElementById("loadMsg");
  msg.textContent = "Loading…";
  const res = await fetch(`/api/load?path=${{encodeURIComponent(p)}}`);
  const data = await res.json();
  if(!res.ok){{ msg.textContent = data.error || "Error"; return; }}
  for(const k of ["title","artist","album","albumartist","involved_people_list","date","genre",
    "year","original_year","track","publisher","comment","artist_sort","albumartist_sort",
    "label","catalog_number",...MB_FIELDS]){{
    if(data[k] !== undefined) setField(k, data[k]);
  }}
  msg.textContent = `Loaded. Art: ${{data.has_art ? "✅ Yes" : "❌ No"}} | ${{(data.length_seconds||0).toFixed(1)}}s | ${{data.bitrate_kbps||0}} kbps | ${{data.sample_rate_hz||0}} Hz`;
}}

async function mbSearch(){{
  const title = getField("title"); const artist = getField("artist"); const album = getField("album");
  const el = document.getElementById("mbResults");
  el.textContent = "Searching…";
  const res = await fetch(`/api/mb_search?title=${{encodeURIComponent(title)}}&artist=${{encodeURIComponent(artist)}}&album=${{encodeURIComponent(album)}}`);
  const data = await res.json();
  if(!data.results){{ el.textContent = data.error || "No results"; return; }}
  window._mb = data.results;
  el.innerHTML = data.results.map((r,i)=>`
    <div class="result-item">
      <strong>${{esc(r.title)}}</strong> — ${{esc(r.artist||"")}} <span style="color:var(--muted)">${{esc(r.date||"")}}</span>
      <button type="button" class="btn btn-sm" onclick="applyMB(${{i}})">Use</button>
      <div class="hint mono">MBID: ${{esc(r.id)}}</div>
    </div>`).join("");
}}
function applyMB(i){{
  const r = window._mb[i]; if(!r) return;
  setField("title", r.title); setField("artist", r.artist);
  if(r.date){{ setField("date", r.date); if(r.date.slice(0,4).match(/^\\d{{4}}$/)) setField("year", r.date.slice(0,4)); }}
}}

async function mbResolve(){{
  const ref = document.getElementById("mbref").value.trim();
  const el = document.getElementById("mbResults");
  el.textContent = "Resolving…";
  const res = await fetch(`/api/mb_resolve?ref=${{encodeURIComponent(ref)}}`);
  const data = await res.json();
  if(!data.fields){{ el.textContent = data.error || "Error"; return; }}
  for(const [k,v] of Object.entries(data.fields)) setField(k, v);
  el.textContent = "Applied fields from MusicBrainz.";
}}

async function discogsSearch(){{
  const artist = getField("albumartist") || getField("artist");
  const album = getField("album");
  const el = document.getElementById("discogsResults");
  const tl = document.getElementById("discogsTracklist");
  el.textContent = "Searching…"; tl.innerHTML = "";
  const res = await fetch(`/api/discogs_search?artist=${{encodeURIComponent(artist)}}&album=${{encodeURIComponent(album)}}`);
  const data = await res.json();
  if(!data.results){{ el.textContent = data.error || "No results"; return; }}
  window._dc = data.results;
  el.innerHTML = data.results.map((r,i)=>`
    <div class="result-item">
      <strong>${{esc(r.title)}}</strong> <span style="color:var(--muted)">${{esc(r.year||"")}}</span>
      <div class="hint">Label: ${{esc(r.label||"")}} | Cat#: ${{esc(r.catno||"")}}</div>
      <button type="button" class="btn btn-sm" onclick="discogsUse(${{i}})">Use + Tracklist</button>
      ${{r.thumb ? `<img src="${{esc(r.thumb)}}" style="max-height:50px;border-radius:6px;margin-left:8px;vertical-align:middle">` : ""}}
    </div>`).join("");
}}
async function discogsUse(i){{
  const r = window._dc[i]; if(!r || !r.id) return;
  const el = document.getElementById("discogsResults");
  const tl = document.getElementById("discogsTracklist");
  el.textContent = "Fetching release…"; tl.innerHTML = "";
  const res = await fetch(`/api/discogs_release?id=${{encodeURIComponent(r.id)}}`);
  const data = await res.json();
  if(!data.fields){{ el.textContent = data.error || "Error"; return; }}
  for(const [k,v] of Object.entries(data.fields)) setField(k, v);
  el.textContent = "Applied fields from Discogs. Pick a track below (optional).";
  const list = data.tracklist || [];
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
  el.textContent = data.note || "Applied parsed fields (verify!)";
}}

async function beatportSearch(){{
  const q = document.getElementById("bpq").value.trim();
  const el = document.getElementById("parseResults");
  const res = await fetch(`/api/beatport_search?q=${{encodeURIComponent(q)}}`);
  const data = await res.json();
  if(data.open_url){{
    el.innerHTML = `Open: <a href="${{esc(data.open_url)}}" target="_blank">${{esc(data.open_url)}}</a><div class="hint">${{esc(data.note||"")}}</div>`;
  }} else {{
    el.textContent = data.error || "Error";
  }}
}}

async function acoustid(){{
  const p = document.getElementById("path").value.trim();
  const el = document.getElementById("acoustidResults");
  el.textContent = "Fingerprinting…";
  const res = await fetch(`/api/acoustid?path=${{encodeURIComponent(p)}}`);
  const data = await res.json();
  if(!data.results){{ el.textContent = data.error || "No results"; return; }}
  window._ac = data.results;
  el.innerHTML = data.results.map((r,i)=>`
    <div class="result-item">
      <strong>${{esc(r.title||"")}}</strong> — ${{esc(r.artist||"")}}
      <span style="color:var(--muted);font-size:.8rem">score: ${{(r.score||0).toFixed(3)}}</span>
      <button type="button" class="btn btn-sm" onclick="useAcoustID(${{i}})">Resolve via MB</button>
      <div class="hint mono">recording_id: ${{esc(r.recording_id||"")}}</div>
    </div>`).join("");
}}
async function useAcoustID(i){{
  const r = window._ac[i]; if(!r || !r.recording_id) return;
  document.getElementById("mbref").value = `https://musicbrainz.org/recording/${{r.recording_id}}`;
  await mbResolve();
}}

async function lastfm(){{
  const artist = getField("artist"); const title = getField("title");
  const el = document.getElementById("lastfmResults");
  el.textContent = "Looking up…";
  const res = await fetch(`/api/lastfm_genre?artist=${{encodeURIComponent(artist)}}&title=${{encodeURIComponent(title)}}`);
  const data = await res.json();
  if(!data.results){{ el.textContent = data.error || "No tags"; return; }}
  el.innerHTML = data.results.map(t => `<button type="button" class="btn btn-sm btn-outline" onclick="setField(\'genre\',${{JSON.stringify(t)}})">${{esc(t)}}</button>`).join("");
}}

let _dirItems = [], _searchItems = [];

document.getElementById("dirList").addEventListener("click", function(e){{
  const item = e.target.closest("[data-idx]");
  if(!item) return;
  const it = _dirItems[parseInt(item.dataset.idx, 10)];
  if(!it) return;
  if(it.type === "dir") openDir(it.path);
  else openFile(it.path);
}});

document.getElementById("sList").addEventListener("click", function(e){{
  const item = e.target.closest("[data-idx]");
  if(!item) return;
  const it = _searchItems[parseInt(item.dataset.idx, 10)];
  if(!it) return;
  openFile(it.path);
}});

document.getElementById("tagForm").addEventListener("submit", async function(e){{
  e.preventDefault();
  const fd = new FormData(e.target);
  if(e.submitter && e.submitter.name) fd.set(e.submitter.name, e.submitter.value);
  try {{
    const res = await fetch("/update", {{method:"POST", body:fd}});
    const data = await res.json();
    if(data.status === "ok") {{
      let html = `<div style="color:#065f46;font-size:1.1rem;font-weight:700;margin-bottom:12px">&#x2705; Tags written successfully</div>`;
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
    }} else {{
      showModal(`<div style="color:#991b1b;font-size:1.1rem;font-weight:700;margin-bottom:12px">&#x274C; Error</div><div>${{esc(data.error || "Unknown error")}}</div>`);
    }}
  }} catch(err) {{
    showModal(`<div style="color:#991b1b">&#x274C; Network error: ${{esc(err.message)}}</div>`);
  }}
}});

function showModal(html) {{
  document.getElementById("resultModalBody").innerHTML = html;
  document.getElementById("resultModal").classList.add("open");
}}

function closeModal() {{
  document.getElementById("resultModal").classList.remove("open");
}}

loadDir();
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
            "artist_sort","albumartist_sort","art_url","label","catalog_number",
            *MB_TXXX_FIELDS,
        ]}

        if request.form.get("auto_artist_sort") and not fields["artist_sort"].strip():
            fields["artist_sort"] = sort_name(fields.get("artist",""))
        if request.form.get("auto_albumartist_sort") and not fields["albumartist_sort"].strip():
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
            "label": fields.get('label', ''),
            "catalog_number": fields.get('catalog_number', ''),
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400

if __name__ == "__main__":

    app.run(host="0.0.0.0", port=5010)
