FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Berlin \
    PYTHONPATH=/app/src

# ffmpeg (mit libass für Untertitel), fette Schrift, espeak-ng als Offline-TTS für --dry-run
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg fonts-montserrat fonts-dejavu-core espeak-ng tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ src/
COPY config/ config/
COPY assets/ assets/
COPY tests/ tests/
COPY pytest.ini .

# UID 1000 = Host-User -> Bind-Mounts (data/, output/) bleiben beschreibbar
RUN useradd -u 1000 -m app && mkdir -p /app/data /app/output /app/credentials \
 && chown -R app:app /app/data /app/output
USER app

ENTRYPOINT ["python", "-m", "shorts"]
CMD ["--help"]
