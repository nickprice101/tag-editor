FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install system packages once at image build time so container restarts do not
# repeatedly mutate dpkg state.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libchromaprint-tools \
        ffmpeg \
        file \
        ca-certificates \
        docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt

RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -f /tmp/requirements.txt

COPY . /app/

CMD ["python", "app.py"]
