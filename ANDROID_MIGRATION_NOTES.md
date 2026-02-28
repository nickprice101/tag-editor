# Android direct-stream migration notes

This repo currently supports the Android direct-stream endpoints used in migration step 3+4:

- `GET /android/library_list.php`
- `POST/GET /android/session_create.php`
- `GET /android/session_get.php`
- `POST/GET /android/stream_sign.php`
- `GET /android/stream.php`
- `POST/GET /android/session_control.php`
- `POST/GET /android/session_queue_replace.php`

## Important caveats for production robustness

1. Session state is in-memory only (`_sessions` dict), so app restart/container reschedule clears playback sessions.
2. `STREAM_SECRET` should always be configured. If unset, code falls back to `dev-insecure-stream-secret`.
3. There is no `/android/library_scan.php` endpoint in the current Flask app; library browsing currently uses filesystem-backed listing via `library_list.php`.
4. Robust seek behavior still depends on proxy/CDN preserving `Range` requests and `206` responses.

## Quick validation

Run:

```bash
pytest -q site/test_signed_stream_flow.py
```

This validates signed URL stream playback, Range support, signature rejection, and session control/queue replacement behavior.
