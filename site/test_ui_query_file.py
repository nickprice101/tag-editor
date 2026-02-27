import app as app_module


def test_ui_home_file_query_sets_path_and_browse_dir(tmp_path):
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
