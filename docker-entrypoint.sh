#!/bin/sh
set -eu

# Installing the engine must not expose an unverified feature. The builder is
# started only after the production feature flag is deliberately enabled.
if [ "${VI_CREATIVE_BUILDER_ENABLED:-false}" = "true" ]; then
  npm exec --no -- hypit version
  npm run start:builder &
  builder_pid=$!

  ready=false
  attempt=0
  while [ "$attempt" -lt 20 ]; do
    if node -e "fetch('http://127.0.0.1:3188/health').then(r => { if (!r.ok) process.exit(1) }).catch(() => process.exit(1))"; then
      ready=true
      break
    fi
    if ! kill -0 "$builder_pid" 2>/dev/null; then
      break
    fi
    attempt=$((attempt + 1))
    sleep 0.5
  done

  if [ "$ready" != "true" ]; then
    echo "Creative builder failed its private readiness check; refusing to expose it." >&2
    exit 1
  fi
fi

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips='*'
