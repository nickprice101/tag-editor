import importlib
import os
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))


def load_real_app():
    for name in ("app", "flask", "requests", "PIL", "PIL.Image", "mutagen.id3", "mutagen.mp3", "acoustid"):
        sys.modules.pop(name, None)
    sys.modules["acoustid"] = types.ModuleType("acoustid")
    return importlib.import_module("app")


def test_ui_home_file_query_sets_path_and_browse_dir(tmp_path):
    app_module = load_real_app()
    music_root = tmp_path / "Music"
    target_dir = music_root / "Downloads" / "youtube-downloads"
    target_dir.mkdir(parents=True)
    target_file = target_dir / "demo.mp3"
    target_file.write_bytes(b"ID3")

    old_root = app_module.MUSIC_ROOT
    old_default = app_module._BROWSE_DEFAULT
    app_module.MUSIC_ROOT = str(music_root)
    app_module._BROWSE_DEFAULT = str(music_root)

    try:
        client = app_module.app.test_client()
        resp = client.get("/", query_string={"file": str(target_file)})
        html = resp.get_data(as_text=True)

        assert resp.status_code == 200
        assert f'id="path" value="{target_file}"' in html
        assert f'id="dir" value="{target_dir}"' in html
    finally:
        app_module.MUSIC_ROOT = old_root
        app_module._BROWSE_DEFAULT = old_default


def test_candidate_requested_paths_preserves_canonical_mount_prefix():
    app_module = load_real_app()
    old_root = app_module.MUSIC_ROOT
    app_module.MUSIC_ROOT = "/mnt/user/media/music"
    try:
        candidates = app_module._candidate_requested_paths(
            "/mnt/user/media/music/Downloads/youtube-downloads/example.mp3"
        )
        assert "/mnt/user/media/music/Downloads/youtube-downloads/example.mp3" in candidates
    finally:
        app_module.MUSIC_ROOT = old_root


def test_ui_home_file_query_with_music_relative_prefix_sets_path_and_browse_dir(tmp_path):
    app_module = load_real_app()
    music_root = tmp_path / "music"
    target_dir = music_root / "Downloads" / "youtube-downloads"
    target_dir.mkdir(parents=True)
    target_file = target_dir / "legacy.mp3"
    target_file.write_bytes(b"ID3")

    old_root = app_module.MUSIC_ROOT
    old_default = app_module._BROWSE_DEFAULT
    app_module.MUSIC_ROOT = str(music_root)
    app_module._BROWSE_DEFAULT = str(music_root)

    try:
        client = app_module.app.test_client()
        alt_prefix_url_path = "/srv/storage/music/Downloads/youtube-downloads/legacy.mp3"
        resp = client.get("/", query_string={"file": alt_prefix_url_path})
        html = resp.get_data(as_text=True)

        assert resp.status_code == 200
        assert f'id="path" value="{target_file}"' in html
        assert f'id="dir" value="{target_dir}"' in html
    finally:
        app_module.MUSIC_ROOT = old_root
        app_module._BROWSE_DEFAULT = old_default


def test_ui_home_uses_split_log_lines_helper_for_yt_dlp_output():
    app_module = load_real_app()
    client = app_module.app.test_client()

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'function splitLogLines(message)' in html
    assert 'replaceAll("\\r", "").split("\\n")' in html
    assert "for (const line of splitLogLines(message)) appendYtDlpLogLine(line);" in html


def test_ui_home_uses_http_url_helper_instead_of_regex_literals():
    app_module = load_real_app()
    client = app_module.app.test_client()

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'function isHttpUrl(value)' in html
    assert 'normalized.startsWith("http://") || normalized.startsWith("https://")' in html
    assert "!d || !isHttpUrl(full)" in html


def test_ui_home_uses_string_helpers_instead_of_inline_regex_literals():
    app_module = load_real_app()
    client = app_module.app.test_client()

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'function stripMp3Suffix(value)' in html
    assert 'function extractLeadingYear(value)' in html
    assert 'function isFourDigitYear(value)' in html
    assert 'function isDigitsOnly(value)' in html
    assert 'function hasZeroTrackTotal(value)' in html
    assert 'function splitWhitespace(value)' in html
    assert 'stripMp3Suffix(fn)' in html
    assert 'splitWhitespace(name)' in html
    assert 'const dateYear = extractLeadingYear(dateVal);' in html
    assert 'if(!trackVal || hasZeroTrackTotal(trackVal)) setField("track", "1/1");' in html
    assert 'if(isDigitsOnly(pos)) setField("track", pos);' in html
    assert 'function normalizeReleasedToIsoDate(value)' in html
    assert 'setField("date", isoReleaseDate);' in html
    assert 'if(getField("year") !== releaseYear) setField("year", releaseYear);' in html


def test_ui_home_init_auto_loads_directory_and_query_file():
    app_module = load_real_app()
    client = app_module.app.test_client()

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "await loadDir();" in html
    assert 'const initialPath = (document.getElementById("path")?.value || "").trim();' in html
    assert 'await requestLoadFile(initialPath, { force: true, reason: "query-file" });' in html
