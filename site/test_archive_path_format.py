from mutagen.id3 import ID3, TALB, TCON, TIT2, TPE2, TRCK, TYER

import app as app_module


def test_archive_uses_album_artist_sort_and_year_in_filename(tmp_path):
    music_root = tmp_path / "Music"
    source_dir = music_root / "Incoming"
    source_dir.mkdir(parents=True)
    mp3_path = source_dir / "track.mp3"

    tags = ID3()
    tags.add(TCON(encoding=3, text=["House"]))
    tags.add(TPE2(encoding=3, text=["The Artist"]))
    tags.add(TALB(encoding=3, text=["The Album"]))
    tags.add(TIT2(encoding=3, text=["The Title"]))
    tags.add(TRCK(encoding=3, text=["1/10"]))
    tags.add(TYER(encoding=3, text=["2023"]))
    tags.save(mp3_path)

    old_root = app_module.MUSIC_ROOT
    app_module.MUSIC_ROOT = str(music_root)

    try:
        archived = app_module.archive_mp3(str(mp3_path))

        expected = music_root / "House" / "Artist, The" / "The Album [2023]" / "The Artist - The Title [2023].mp3"
        assert archived == str(expected)
        assert expected.exists()
        assert not mp3_path.exists()
    finally:
        app_module.MUSIC_ROOT = old_root


def test_archive_non_album_tracks_go_to_albumartist_base_folder(tmp_path):
    music_root = tmp_path / "Music"
    source_dir = music_root / "Incoming"
    source_dir.mkdir(parents=True)
    mp3_path = source_dir / "single.mp3"

    tags = ID3()
    tags.add(TCON(encoding=3, text=["House"]))
    tags.add(TPE2(encoding=3, text=["The Artist"]))
    tags.add(TALB(encoding=3, text=["[non-album tracks]"]))
    tags.add(TIT2(encoding=3, text=["The Title"]))
    tags.add(TRCK(encoding=3, text=["1/10"]))
    tags.add(TYER(encoding=3, text=["2023"]))
    tags.save(mp3_path)

    old_root = app_module.MUSIC_ROOT
    app_module.MUSIC_ROOT = str(music_root)

    try:
        archived = app_module.archive_mp3(str(mp3_path))

        expected = music_root / "House" / "Artist, The" / "The Artist - The Title [2023].mp3"
        assert archived == str(expected)
        assert expected.exists()
        assert not mp3_path.exists()
    finally:
        app_module.MUSIC_ROOT = old_root


def test_archive_triggers_android_library_scan_for_source_and_destination_dirs(tmp_path, monkeypatch):
    music_root = tmp_path / "Music"
    source_dir = music_root / "Incoming"
    source_dir.mkdir(parents=True)
    mp3_path = source_dir / "track.mp3"

    tags = ID3()
    tags.add(TCON(encoding=3, text=["House"]))
    tags.add(TPE2(encoding=3, text=["The Artist"]))
    tags.add(TALB(encoding=3, text=["The Album"]))
    tags.add(TIT2(encoding=3, text=["The Title"]))
    tags.add(TRCK(encoding=3, text=["1/10"]))
    tags.add(TYER(encoding=3, text=["2023"]))
    tags.save(mp3_path)

    old_root = app_module.MUSIC_ROOT
    old_scan_base = app_module.ANDROID_LIBRARY_SCAN_BASE_URL
    app_module.MUSIC_ROOT = str(music_root)
    app_module.ANDROID_LIBRARY_SCAN_BASE_URL = "http://scanner.test"
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append((url, params, timeout, headers))
        return object()

    monkeypatch.setattr(app_module.requests, "get", fake_get)

    try:
        archived = app_module.archive_mp3(str(mp3_path))

        destination_dir = str((tmp_path / "Music" / "House" / "Artist, The" / "The Album [2023]"))
        assert archived == str((tmp_path / "Music" / "House" / "Artist, The" / "The Album [2023]" / "The Artist - The Title [2023].mp3"))
        assert calls == [
            (
                "http://scanner.test/android/library_scan.php",
                {"async": "1", "path": str(source_dir)},
                3,
                {"User-Agent": app_module.UA},
            ),
            (
                "http://scanner.test/android/library_scan.php",
                {"async": "1", "path": destination_dir},
                3,
                {"User-Agent": app_module.UA},
            ),
        ]
    finally:
        app_module.MUSIC_ROOT = old_root
        app_module.ANDROID_LIBRARY_SCAN_BASE_URL = old_scan_base
