# tag-editor
ID3 tag editor using acoustic ID, discogs and beatport lookups, and URL parser. 
Includes filter and search functionality.

Container stack setup below. If you deploy through Portainer, do not paste only the YAML from this README into the web editor and expect `build.context: .` to find the repo's `Dockerfile`; Portainer will build from its temporary stack directory instead. Use the checked-out repo with the included `docker-compose.yml`, or point the stack's build context at the real repo path on disk.

This deployment expects a flat app directory on the Docker host at `/opt/beets/tag-editor`, with files such as `Dockerfile`, `requirements.txt`, and `app.py` living directly in that directory. The repository may keep application files under `site/` for organization, but the deployed NAS directory should stay flat.

```yaml
services:
  tag-editor:
    build:
      context: .
      dockerfile: Dockerfile
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

The repo now includes [`docker-compose.yml`](C:\Users\nickp\github\tag-editor\docker-compose.yml), so the simplest path is:

```powershell
cd /opt/beets/tag-editor
docker compose build
docker compose up -d
```

If you prefer Portainer stacks, deploy from the repo directory or Git repository so the build context contains both `docker-compose.yml` and `Dockerfile`.

If you want to paste a stack straight into Portainer's web editor, use [`portainer-stack.yml`](C:\Users\nickp\github\tag-editor\portainer-stack.yml). That version uses an absolute build context (`/opt/beets/tag-editor`) so Portainer builds from the real repo on disk instead of its temporary stack directory.

If you want the footer's "YouTube liked playlist import" action to work, keep the helper script outside this repo and mount it into `/app/scripts/yt_dlp.sh`. The app now starts that script server-side and streams live output into the page over SSE; the script still needs Docker socket access so `docker exec yt-dlp-webui ...` can reach the other container.

If you have already hit `E: dpkg was interrupted`, rebuild the image and recreate the container instead of retrying the old startup command. The broken state was caused by installing Debian packages during container boot; the `Dockerfile` above moves that work to build time so normal restarts stay clean.
