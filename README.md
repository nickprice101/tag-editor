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

      # OPTIONAL: enable these lookups by adding keys/tokens
      - DISCOGS_TOKEN=xxxx
      - ACOUSTID_KEY=xxxx
      - LASTFM_API_KEY=xxx

    volumes:
      # App code + requirements live here on the NAS
      - /opt/beets/tag-editor:/app

      # Music library read/write (needed for tag writes + archive move)
      - /mnt/HD/HD_a2/Media/Music:/mnt/HD/HD_a2/Media/Music

    ports:
      - "5010:5010"

    # Install Chromaprint (fpcalc) for AcoustID, then Python deps, then run app
    command: >
      sh -lc "set -e;
              apt-get update;
              apt-get install -y --no-install-recommends libchromaprint-tools ffmpeg file ca-certificates;
              rm -rf /var/lib/apt/lists/*;
              pip install --no-cache-dir -r requirements.txt;
              python app.py"
    restart: unless-stopped
```
