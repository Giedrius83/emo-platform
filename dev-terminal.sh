#!/usr/bin/env bash
# Start the trading terminal: FastAPI telemetry service + Vite dev server.
# Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")"

API_DIR="services/telemetry"
UI_DIR="apps/trading-terminal"
API_PORT="${TELEMETRY_PORT:-8000}"

if [ ! -d "$API_DIR/.venv" ]; then
  echo "==> creating python venv"
  python3 -m venv "$API_DIR/.venv"
  "$API_DIR/.venv/bin/pip" install -q -r "$API_DIR/requirements.txt"
fi

if [ ! -d "$UI_DIR/node_modules" ]; then
  echo "==> installing node dependencies"
  (cd "$UI_DIR" && npm install)
fi

pids=()
cleanup() {
  trap - INT TERM EXIT
  for pid in "${pids[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

echo "==> telemetry service on http://127.0.0.1:${API_PORT}"
"$API_DIR/.venv/bin/python" -m uvicorn app.main:app \
  --host 127.0.0.1 --port "$API_PORT" --app-dir "$API_DIR" &
pids+=($!)

echo "==> dashboard on http://127.0.0.1:5173"
(cd "$UI_DIR" && TELEMETRY_ORIGIN="http://127.0.0.1:${API_PORT}" npm run dev) &
pids+=($!)

wait
