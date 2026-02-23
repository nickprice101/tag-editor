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
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN", "").strip()
ACOUSTID_KEY = os.getenv("ACOUSTID_KEY", "").strip()
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "").strip()

UA = "CorsicanEscapeTagEditor/2.0 (nick@corsicanescape.com)"

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
                entries.append({"type": "file", "name": e.name, "path": full, "rel": rel})
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
                results.append({"type": "file", "name": fn, "path": full, "rel": rel})
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

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>MP3 Tag Editor</title>
  <style>
    body{{font-family:system-ui,Segoe UI,Arial;margin:20px;max-width:1250px}}
    input,textarea{{width:100%;padding:10px;margin:6px 0 14px 0}}
    label{{font-weight:650}}
    .row{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
    .btn{{padding:10px 14px;font-weight:750;margin-right:8px;margin-top:6px}}
    .hint{{color:#555;font-size:13px;margin-top:-10px;margin-bottom:12px}}
    .box{{border:1px solid #ddd;border-radius:12px;padding:12px;margin:10px 0}}
    .mono{{font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace}}
    .list{{max-height:340px;overflow:auto;border:1px solid #eee;border-radius:12px;padding:10px}}
    .item{{padding:6px 8px;border-radius:10px;display:flex;gap:10px;align-items:center}}
    .item:hover{{background:#f6f6f6}}
    .tag{{font-size:12px;background:#f1f1f1;border-radius:999px;padding:2px 8px}}
    a{{text-decoration:none}}
  </style>
</head>
<body>
<h2>MP3 ID3v2 Editor</h2>
<div class="hint">Root: <span class="mono">{MUSIC_ROOT}</span></div>

<div class="row">
  <div class="box">
    <b>Browse</b>
    <div class="hint">Navigate folders and click a file to edit.</div>
    <label>Directory</label>
    <input id="dir" value="{MUSIC_ROOT}"/>
    <label>Filter (optional, current dir)</label>
    <input id="dirFilter" placeholder="e.g. maribou or 2024"/>
    <button class="btn" type="button" onclick="loadDir()">Load</button>
    <button class="btn" type="button" onclick="upDir()">Up</button>
    <div id="dirErr" class="hint"></div>
    <div class="list" id="dirList"></div>
  </div>

  <div class="box">
    <b>Search</b>
    <div class="hint">Search recursively under a directory.</div>
    <label>Search root</label>
    <input id="sroot" value="{MUSIC_ROOT}"/>
    <label>Query</label>
    <input id="sq" placeholder="filename or partial path"/>
    <button class="btn" type="button" onclick="doSearch()">Search</button>
    <div id="sErr" class="hint"></div>
    <div class="list" id="sList"></div>
  </div>
</div>

<div class="box">
  <b>Edit</b>
  <div class="hint">Single file editor. Use “Load existing tags” then tweak and write. Archive uses your structure with <span class="mono">Album [Year]</span>.</div>

  <form method="POST" action="/update">
    <label>File path</label>
    <input name="path" id="path" value="{path}"/>

    <button type="button" class="btn" onclick="loadTags()">Load existing tags + audio info</button>
    <div id="loadMsg" class="hint"></div>

    <div class="box">
      <b>Lookups</b>

      <div class="row">
        <div>
          <button type="button" class="btn" onclick="mbSearch()">MusicBrainz Search</button>
          <input id="mbref" placeholder="Paste MusicBrainz URL or MBID (recording/release)"/>
          <button type="button" class="btn" onclick="mbResolve()">Resolve MusicBrainz URL/MBID</button>
          <div id="mbResults"></div>
        </div>

        <div>
          <button type="button" class="btn" onclick="discogsSearch()">Discogs Search (album)</button>
          <div class="hint">Needs DISCOGS_TOKEN env. Includes tracklist picker.</div>
          <div id="discogsResults"></div>
          <div id="discogsTracklist"></div>
        </div>
      </div>

      <div class="row">
        <div>
          <input id="purl" placeholder="Paste URL: Beatport / Bandcamp / Juno / Traxsource"/>
          <button type="button" class="btn" onclick="parseUrl()">Parse URL</button>
          <input id="bpq" placeholder="Beatport search query (optional)"/>
          <button type="button" class="btn" onclick="beatportSearch()">Beatport Search</button>
          <div id="parseResults"></div>
        </div>

        <div>
          <button type="button" class="btn" onclick="acoustid()">AcoustID Fingerprint</button>
          <div class="hint">Needs ACOUSTID_KEY env.</div>
          <div id="acoustidResults"></div>

          <button type="button" class="btn" onclick="lastfm()">Last.fm Genre Suggest</button>
          <div class="hint">Needs LASTFM_API_KEY env.</div>
          <div id="lastfmResults"></div>
        </div>
      </div>
    </div>

    <div class="row">
      <div>
        <label>Title</label><input name="title"/>
        <label>Artist</label><input name="artist"/>
        <label>Album</label><input name="album"/>
        <label>Album Artist (band)</label><input name="albumartist"/>
        <label>Involved people list</label>
        <input name="involved_people_list" placeholder="Britney Spears, Christina Aguilera"/>
        <div class="hint">Stored as <span class="mono">TXXX:involved_people_list</span>.</div>
      </div>
      <div>
        <label>Date</label><input name="date" placeholder="YYYY or YYYY-MM-DD"/>
        <label>Year</label><input name="year" placeholder="YYYY"/>
        <label>Original Year</label><input name="original_year" placeholder="YYYY"/>
        <label>Genre</label><input name="genre"/>
        <label>Track number</label><input name="track" placeholder="1 or 1/12"/>
        <label>Publisher</label><input name="publisher"/>
        <label>Comment</label><textarea name="comment" rows="4"></textarea>
      </div>
    </div>

    <div class="row">
      <div>
        <label>Artist sort</label><input name="artist_sort" placeholder="Beatles, The"/>
        <div class="hint">Auto-generates only for names starting with “The …”.</div>
        <label><input type="checkbox" name="auto_artist_sort" value="1" checked /> Auto-generate Artist sort if blank</label>
      </div>
      <div>
        <label>Album artist sort</label><input name="albumartist_sort" placeholder="Beatles, The"/>
        <div class="hint">Auto-generates only for names starting with “The …”.</div>
        <label><input type="checkbox" name="auto_albumartist_sort" value="1" checked /> Auto-generate Album artist sort if blank</label>
      </div>
    </div>

    <div class="row">
      <div>
        <label>Label</label><input name="label" placeholder="(optional)"/>
        <div class="hint">Stored as <span class="mono">TXXX:label</span></div>
      </div>
      <div>
        <label>Catalog number</label><input name="catalog_number" placeholder="(optional)"/>
        <div class="hint">Stored as <span class="mono">TXXX:catalog_number</span></div>
      </div>
    </div>

    <label>Cover art image URL (optional)</label>
    <input name="art_url" placeholder="https://.../cover.jpg or .png (embedded as JPEG)"/>

    <button class="btn" type="submit" name="action" value="write">Write tags</button>
    <button class="btn" type="submit" name="action" value="archive">Write tags + Archive this file</button>
  </form>
</div>

<script>
function esc(s){{ return (s||"").toString().replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;"); }}
function setField(name, val){{ const el = document.querySelector(`[name="${{name}}"]`); if(el) el.value = val || ""; }}
function getField(name){{ const el = document.querySelector(`[name="${{name}}"]`); return el ? el.value : ""; }}

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
  box.innerHTML = data.items.map(it => {{
    const icon = it.type === "dir" ? "📁" : "🎵";
    const action = it.type === "dir"
      ? `onclick="openDir('${{esc(it.path)}}')"`
      : `onclick="openFile('${{esc(it.path)}}')"`
    return `<div class="item" ${{action}}><span>${{icon}}</span><span style="flex:1">${{esc(it.name)}}</span><span class="tag mono">${{esc(it.rel)}}</span></div>`;
  }}).join("");
}}

function openDir(p){{ document.getElementById("dir").value = p; loadDir(); }}
function openFile(p){{ document.getElementById("path").value = p; window.scrollTo(0, document.body.scrollHeight); }}

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
  box.innerHTML = data.results.map(it =>
    `<div class="item" onclick="openFile('${{esc(it.path)}}')"><span>🎵</span><span style="flex:1">${{esc(it.name)}}</span><span class="tag mono">${{esc(it.rel)}}</span></div>`
  ).join("");
}}

async function loadTags(){{
  const p = document.getElementById("path").value.trim();
  const msg = document.getElementById("loadMsg");
  msg.textContent = "Loading…";
  const res = await fetch(`/api/load?path=${{encodeURIComponent(p)}}`);
  const data = await res.json();
  if(!res.ok){{ msg.textContent = data.error || "Error"; return; }}

  for(const k of ["title","artist","album","albumartist","involved_people_list","date","genre","year","original_year","track","publisher","comment","artist_sort","albumartist_sort","label","catalog_number"]){{
    if(data[k] !== undefined) setField(k, data[k]);
  }}
  msg.textContent = `Loaded. Art: ${{data.has_art ? "Yes" : "No"}} | Length: ${{(data.length_seconds||0).toFixed(1)}}s | Bitrate: ${{data.bitrate_kbps||0}}kbps | SR: ${{data.sample_rate_hz||0}}Hz`;
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
    <div class="box">
      <b>${{esc(r.title)}}</b> — ${{esc(r.artist||"")}} <span style="color:#666">${{esc(r.date||"")}}</span>
      <button type="button" class="btn" onclick="applyMB(${{i}})">Use</button>
      <div class="hint">MBID: <span class="mono">${{esc(r.id)}}</span></div>
    </div>
  `).join("");
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
    <div class="box">
      <b>${{esc(r.title)}}</b> <span style="color:#666">${{esc(r.year||"")}}</span>
      <div class="hint">Label: ${{esc(r.label||"")}} | Cat#: ${{esc(r.catno||"")}}</div>
      <button type="button" class="btn" onclick="discogsUse(${{i}})">Use + Tracklist</button>
      ${{r.thumb ? `<img src="${{esc(r.thumb)}}" style="max-height:60px;border-radius:8px;margin-left:8px">` : ""}}
    </div>
  `).join("");
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
  tl.innerHTML = "<div class='box'><b>Discogs Tracklist (click to apply title/track)</b>" +
    list.map(t => {{
      const pos = (t.position||"").trim();
      return `<div class="item" onclick="applyTrack('${{esc(pos)}}','${{esc(t.title||"")}}')">
        <span class="tag mono">${{esc(pos||"")}}</span><span style="flex:1">${{esc(t.title||"")}}</span><span class="hint">${{esc(t.duration||"")}}</span>
      </div>`;
    }}).join("") + "</div>";
}}
function applyTrack(pos, title){{
  // Try to extract a numeric track from position (e.g. "A1" => 1 is ambiguous; only use numeric if clean)
  const m = (pos||"").match(/^(\\d+)$/);
  if(m) setField("track", m[1]);
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
    <div class="box">
      <b>${{esc(r.title||"")}}</b> — ${{esc(r.artist||"")}} <span style="color:#666">score: ${{(r.score||0).toFixed(3)}}</span>
      <button type="button" class="btn" onclick="useAcoustID(${{i}})">Resolve via MusicBrainz</button>
      <div class="hint">recording_id: <span class="mono">${{esc(r.recording_id||"")}}</span></div>
    </div>
  `).join("");
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
  el.innerHTML = data.results.map(t => `<button type="button" class="btn" onclick="setField('genre','${{esc(t)}}')">${{esc(t)}}</button>`).join("");
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
            "artist_sort","albumartist_sort","art_url","label","catalog_number"
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

        msg = f"OK ✅\nWrote tags to:\n{path}\n\n"
        if archived_to:
            msg += f"Archived to:\n{archived_to}\n\n"
        msg += f"Involved people list: {normalize_involved_people(fields.get('involved_people_list',''))}\n"
        msg += f"Label: {fields.get('label','')}\n"
        msg += f"Catalog #: {fields.get('catalog_number','')}\n"
        return f"<pre>{msg}</pre><p><a href='/?path={requests.utils.quote(path)}'>Back</a></p>"
    except Exception as e:
        return f"<pre>ERROR ❌\n{e}\n</pre><p><a href='/'>Back</a></p>", 400

if __name__ == "__main__":

    app.run(host="0.0.0.0", port=5010)
