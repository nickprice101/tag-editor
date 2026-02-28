import app as app_module


def test_signed_stream_flow(tmp_path, monkeypatch):
    music_root = tmp_path / "music"
    music_root.mkdir()
    track = music_root / "demo.mp3"
    payload = b"ID3" + b"\x00" * 4096
    track.write_bytes(payload)

    monkeypatch.setattr(app_module, "MUSIC_ROOT", str(music_root))
    monkeypatch.setattr(app_module, "STREAM_SECRET", "unit-test-secret")

    client = app_module.app.test_client()

    create_res = client.post(
        "/android/session_create.php",
        json={"paths": [str(track)], "active_index": 0},
    )
    assert create_res.status_code == 200
    session_id = create_res.get_json()["session_id"]

    get_res = client.get(f"/android/session_get.php?session_id={session_id}")
    assert get_res.status_code == 200
    assert get_res.get_json()["active_track"] == str(track)

    sign_res = client.post("/android/stream_sign.php", json={"session_id": session_id, "ttl": 300})
    assert sign_res.status_code == 200
    stream_url = sign_res.get_json()["stream_url"]

    full_stream = client.get(stream_url)
    assert full_stream.status_code == 200
    assert full_stream.data == payload

    range_stream = client.get(stream_url, headers={"Range": "bytes=0-9"})
    assert range_stream.status_code == 206
    assert range_stream.data == payload[:10]


def test_signed_stream_rejects_invalid_signature(tmp_path, monkeypatch):
    music_root = tmp_path / "music"
    music_root.mkdir()
    track = music_root / "demo.mp3"
    track.write_bytes(b"ID3" + b"\x00" * 32)

    monkeypatch.setattr(app_module, "MUSIC_ROOT", str(music_root))
    monkeypatch.setattr(app_module, "STREAM_SECRET", "unit-test-secret")

    client = app_module.app.test_client()
    create_res = client.post("/android/session_create.php", json={"paths": [str(track)]})
    session_id = create_res.get_json()["session_id"]

    sign_res = client.post("/android/stream_sign.php", json={"session_id": session_id, "ttl": 300})
    stream_url = sign_res.get_json()["stream_url"]
    bad_url = stream_url.replace("sig=", "sig=bad")

    denied = client.get(bad_url)
    assert denied.status_code == 401
