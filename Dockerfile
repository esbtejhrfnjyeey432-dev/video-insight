FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# yt-dlp may need the system ffmpeg binary when it has to merge separate
# video/audio streams downloaded from a source platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy every Python module (app, resolver, quota_identity, payment_store and
# any future ones) so a newly added module can never be missing in the image.
COPY --chown=appuser:appuser *.py ./
COPY --chown=appuser:appuser static ./static

USER appuser

EXPOSE 8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
