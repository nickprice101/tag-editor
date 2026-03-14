# tag-editor
ID3 tag editor using acoustic ID, discogs and beatport lookups, and URL parser. 
Includes filter and search functionality.

Container stack setup below.

```
services:
  tag-editor:
    image: python:3.11-slim-bookworm
    container_name: tag-editor
    working_dir: /app
    environment:
      - TZ=Europe/Amsterdam

      # Optional extra auth layer (recommended if exposed beyond LAN; Cloudflare Access also OK)
      #- APP_USER=xxxx
      #- APP_PASS=xxxx

      # REQUIRED: your music root
      - MUSIC_ROOT=/mnt/HD/HD_a2/Media/Music
      # Optional override if the helper script lives elsewhere in the container
      - YT_DLP_SCRIPT=/app/scripts/yt_dlp.sh

      # OPTIONAL: enable these lookups by adding keys/tokens
      - DISCOGS_TOKEN=xxxx
      - ACOUSTID_KEY=xxxx
      # No longer implemented as didn't add enough benefit.
      #- LASTFM_API_KEY=xxx

    volumes:
      # App code + requirements live here on the NAS
      - /opt/beets/tag-editor:/app
      # Helper scripts live outside the repo and are mounted read-only
      - /opt/beets/scripts:/app/scripts:ro

      # Music library read/write (needed for tag writes + archive move)
      # Paths should be the same inside and outside the container.
      - /mnt/HD/HD_a2/Media/Music:/mnt/HD/HD_a2/Media/Music
      # Allow the app container to call docker exec against sibling containers
      - /var/run/docker.sock:/var/run/docker.sock

    ports:
      - "5010:5010"

    # Install Chromaprint (fpcalc) for AcoustID, then Python deps, then run app
    command: >
      sh -lc "set -e;
              apt-get update;
              apt-get install -y --no-install-recommends libchromaprint-tools ffmpeg file ca-certificates docker.io;
              rm -rf /var/lib/apt/lists/*;
              pip install --no-cache-dir -r requirements.txt;
              python app.py"
    restart: unless-stopped
```

If you want the footer's "YouTube liked playlist import" action to work, keep the helper script outside this repo and mount it into `/app/scripts/yt_dlp.sh`. The app now starts that script server-side and streams live output into the page over SSE; the script still needs Docker socket access so `docker exec yt-dlp-webui ...` can reach the other container.
