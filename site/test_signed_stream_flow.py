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


def test_session_control_and_queue_replace(tmp_path, monkeypatch):
    music_root = tmp_path / "music"
    music_root.mkdir()
    track_a = music_root / "a.mp3"
    track_b = music_root / "b.mp3"
    track_c = music_root / "c.mp3"
    for t in (track_a, track_b, track_c):
        t.write_bytes(b"ID3" + b"\x00" * 16)

    monkeypatch.setattr(app_module, "MUSIC_ROOT", str(music_root))
    monkeypatch.setattr(app_module, "STREAM_SECRET", "unit-test-secret")

    client = app_module.app.test_client()
    create = client.post(
        "/android/session_create.php",
        json={"paths": [str(track_a), str(track_b)], "active_index": 0},
    )
    assert create.status_code == 200
    session_id = create.get_json()["session_id"]

    play = client.post("/android/session_control.php", json={"session_id": session_id, "action": "play"})
    assert play.status_code == 200
    assert play.get_json()["playing"] is True

    seek = client.post(
        "/android/session_control.php",
        json={"session_id": session_id, "action": "seek", "position_ms": 42000},
    )
    assert seek.status_code == 200
    assert seek.get_json()["position_ms"] == 42000

    nxt = client.post("/android/session_control.php", json={"session_id": session_id, "action": "next"})
    assert nxt.status_code == 200
    assert nxt.get_json()["active_index"] == 1
    assert nxt.get_json()["position_ms"] == 0

    replace = client.post(
        "/android/session_queue_replace.php",
        json={"session_id": session_id, "paths": [str(track_c)], "active_index": 0},
    )
    assert replace.status_code == 200
    data = replace.get_json()
    assert data["queue_count"] == 1
    assert data["active_track"] == str(track_c)
    assert data["position_ms"] == 0
