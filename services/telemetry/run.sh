#!/usr/bin/env bash
# Start the telemetry service. Override host/port with TELEMETRY_HOST / TELEMETRY_PORT.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m uvicorn app.main:app \
  --host "${TELEMETRY_HOST:-0.0.0.0}" \
  --port "${TELEMETRY_PORT:-8000}" \
  "$@"
