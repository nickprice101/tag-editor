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


def test_ui_home_keeps_yt_dlp_log_split_regex_on_one_line():
    app_module = load_real_app()
    client = app_module.app.test_client()

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "message.split(/\\r?\\n/)" in html


def test_ui_home_keeps_http_url_regex_escaped():
    app_module = load_real_app()
    client = app_module.app.test_client()

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "!/^https?:\\/\\//i.test(full)" in html
