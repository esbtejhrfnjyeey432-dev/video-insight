FROM node:22.15.0-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:/app/node_modules/.bin:${PATH}" \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY requirements.txt ./
RUN python3 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir -r requirements.txt

COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund \
    && npm exec --no -- hypit version

COPY --chown=appuser:appuser app.py resolver.py ./
COPY --chown=appuser:appuser static ./static
COPY --chown=appuser:appuser src ./src
COPY --chown=appuser:appuser examples ./examples
COPY --chown=appuser:appuser docker-entrypoint.sh ./docker-entrypoint.sh

RUN chmod +x ./docker-entrypoint.sh \
    && mkdir -p data/uploads data/svml data/runs data/outputs \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080
CMD ["./docker-entrypoint.sh"]
