#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
MODE=${1:-offline}
PYTHON=${DYOPS_PYTHON:-"$ROOT_DIR/dyops_core/.venv/bin/python"}

case "$MODE" in
  offline)
    export DYOPS_OFFLINE_MODE=1
    export DYOPS_FEED_DISABLED=0
    export DYOPS_BINANCE_FEED=stable
    ;;
  feed-off)
    export DYOPS_OFFLINE_MODE=0
    export DYOPS_FEED_DISABLED=1
    export DYOPS_BINANCE_FEED=stable
    ;;
  live)
    export DYOPS_OFFLINE_MODE=0
    export DYOPS_FEED_DISABLED=0
    case "${DYOPS_BINANCE_FEED:-stable}" in
      off|none|disabled) export DYOPS_BINANCE_FEED=stable ;;
      *) export DYOPS_BINANCE_FEED=${DYOPS_BINANCE_FEED:-stable} ;;
    esac
    ;;
  *)
    printf 'Usage: %s [offline|feed-off|live]\n' "$0" >&2
    exit 2
    ;;
esac

if command -v docker >/dev/null 2>&1; then
  printf 'Docker is available but not required; using the native local path.\n'
else
  printf 'Docker not found; continuing with the native local path.\n'
fi

if [ ! -x "$PYTHON" ]; then
  printf 'Python environment not found at %s\n' "$PYTHON" >&2
  printf 'Prepare it with:\n' >&2
  printf '  cd dyops_core && python3 -m venv .venv && .venv/bin/pip install -U pip maturin\n' >&2
  printf '  .venv/bin/pip install -e . && .venv/bin/maturin develop --release\n' >&2
  printf '  cd .. && dyops_core/.venv/bin/pip install -r backend/requirements.txt\n' >&2
  exit 1
fi
if ! "$PYTHON" -c 'import dyops_core, fastapi, uvicorn' >/dev/null 2>&1; then
  printf 'Native extension or API dependencies are missing; run the setup commands above.\n' >&2
  exit 1
fi
command -v npm >/dev/null 2>&1 || {
  printf 'npm is required for the React reference UI.\n' >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || {
  printf 'curl is required for the local health check.\n' >&2
  exit 1
}
command -v setsid >/dev/null 2>&1 || {
  printf 'setsid is required to supervise local API/UI process groups.\n' >&2
  exit 1
}
if ! "$PYTHON" - <<'PY'
import socket

for port in (8000, 5173):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
PY
then
  printf 'Ports 8000 and 5173 must be free before starting the local demo.\n' >&2
  exit 1
fi
if [ ! -d "$ROOT_DIR/frontend/node_modules" ]; then
  printf 'Frontend dependencies are missing. Run: cd frontend && npm ci\n' >&2
  exit 1
fi

export DYOPS_DEMO_INJECT=1
export DYOPS_DEMO_SECRET=${DYOPS_DEMO_SECRET:-dyops-local-demo}
export DYOPS_CORS_ORIGINS=${DYOPS_CORS_ORIGINS:-http://127.0.0.1:5173}
export PYTHONPATH="$ROOT_DIR:$ROOT_DIR/dyops_core${PYTHONPATH:+:$PYTHONPATH}"

cleanup() {
  trap - EXIT INT TERM
  [ -n "${API_PID:-}" ] && kill -- "-$API_PID" 2>/dev/null || true
  [ -n "${UI_PID:-}" ] && kill -- "-$UI_PID" 2>/dev/null || true
  wait "${API_PID:-}" "${UI_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$ROOT_DIR"
setsid "$PYTHON" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 &
API_PID=$!
setsid bash -c 'cd "$1/frontend" && exec npm run dev -- --host 127.0.0.1' bash "$ROOT_DIR" &
UI_PID=$!

printf '\nWaiting for local API'
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/api/status >/dev/null 2>&1; then
    break
  fi
  printf '.'
  sleep 1
done
if ! curl -fsS http://127.0.0.1:8000/api/status >/dev/null 2>&1; then
  printf '\nLocal API did not become ready.\n' >&2
  exit 1
fi

printf '\n\nDyops native partner demo is ready:\n'
printf '  Mode:     %s\n' "$MODE"
printf '  UI:       http://127.0.0.1:5173\n'
printf '  API docs: http://127.0.0.1:8000/docs\n'
printf '  Status:   http://127.0.0.1:8000/api/status\n'
printf '  Secret:   %s\n' "$DYOPS_DEMO_SECRET"
printf '  Inject:   curl -X POST -H "X-Dyops-Demo-Secret: %s" "http://127.0.0.1:8000/api/demo/inject_scenario?name=sudden_depeg&seed=13"\n' "$DYOPS_DEMO_SECRET"
printf '  Reset:    curl -X POST -H "X-Dyops-Demo-Secret: %s" "http://127.0.0.1:8000/api/demo/reset"\n' "$DYOPS_DEMO_SECRET"
printf '\nDocker is packaging; the engine runs without it. Press Ctrl+C to stop.\n'

wait -n "$API_PID" "$UI_PID"
