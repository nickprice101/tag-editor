# tag-editor
ID3 tag editor using acoustic ID, discogs and beatport lookups, and URL parser. 
Includes filter and search functionality.

Container stack setup below.

```
services:
  tag-editor:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: tag-editor
    working_dir: /app/site
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
      - DISCOGS_TOKEN=XXXXXX
      - ACOUSTID_KEY=XXXXXX

      # Headless fallback (Playwright)
      - HEADLESS_ENABLED=1
      - HEADLESS_TIMEOUT_SECS=20

      # Persist Playwright browsers (so you don't re-download every restart)
      - PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

    volumes:
      # Repo root lives here on the NAS
      - /opt/beets/tag-editor:/app
      # Playwright browser support
      - tag_editor_playwright_browsers:/ms-playwright
      # Helper scripts live outside the repo and are mounted read-only
      - /opt/beets/scripts:/app/scripts:ro
      # Music library read/write (needed for tag writes + archive move)
      # Paths should be the same inside and outside the container.
      - /mnt/HD/HD_a2/Media/Music:/mnt/HD/HD_a2/Media/Music
      # Allow the app container to call docker exec against sibling containers
      - /var/run/docker.sock:/var/run/docker.sock

    ports:
      - "5010:5010"

    # Runtime only starts the app; system packages and Playwright are baked into the image.
    command: python app.py
    restart: unless-stopped
    
volumes:
  tag_editor_playwright_browsers:
```

If you want the footer's "YouTube liked playlist import" action to work, keep the helper script outside this repo and mount it into `/app/scripts/yt_dlp.sh`. The app now starts that script server-side and streams live output into the page over SSE; the script still needs Docker socket access so `docker exec yt-dlp-webui ...` can reach the other container.

If you have already hit `E: dpkg was interrupted`, rebuild the image and recreate the container instead of retrying the old startup command. The broken state was caused by installing Debian packages during container boot; the `Dockerfile` above moves that work to build time so normal restarts stay clean.
