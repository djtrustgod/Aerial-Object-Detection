#!/bin/sh
set -e

# Create data directories if missing
mkdir -p /app/config /app/data/clips /app/data/db /app/data/logs

# Copy default config on first run
if [ ! -f /app/config/default.yaml ]; then
    cp /app/config-default/default.yaml /app/config/default.yaml
    echo "Initialized default config from image defaults."
fi

exec python -m src.main "$@"
