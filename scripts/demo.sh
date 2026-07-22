#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

MODE=${1:-live}
case "$MODE" in
  live)
    export DYOPS_OFFLINE_MODE=0
    ;;
  offline)
    export DYOPS_OFFLINE_MODE=1
    ;;
  *)
    printf 'Usage: %s [live|offline]\n' "$0" >&2
    exit 2
    ;;
esac

command -v docker >/dev/null 2>&1 || {
  printf 'Docker is required. Install Docker Engine/Desktop with Compose support.\n' >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  printf 'Docker Compose plugin is required.\n' >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || {
  printf 'curl is required for the demo health check.\n' >&2
  exit 1
}

export DYOPS_DEMO_INJECT=1
export DYOPS_DEMO_SECRET=${DYOPS_DEMO_SECRET:-dyops-local-demo}
docker compose up --build -d

printf '\nWaiting for the API health check'
attempt=0
until curl -fsS http://localhost:8000/api/status >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    printf '\nAPI did not become healthy; inspect: docker compose logs api\n' >&2
    exit 1
  fi
  printf '.'
  sleep 2
done

printf '\nDyops partner demo is starting:\n'
printf '  Mode:     %s\n' "$MODE"
printf '  UI:       http://localhost:8080\n'
printf '  API docs: http://localhost:8000/docs\n'
printf '  Status:   http://localhost:8000/api/status\n'
printf '  Injection secret: %s\n' "$DYOPS_DEMO_SECRET"
printf '  Inject:   curl -X POST -H "X-Dyops-Demo-Secret: %s" "http://localhost:8000/api/demo/inject_scenario?name=sudden_depeg&seed=13"\n' "$DYOPS_DEMO_SECRET"
printf '  Reset:    curl -X POST -H "X-Dyops-Demo-Secret: %s" "http://localhost:8000/api/demo/reset"\n' "$DYOPS_DEMO_SECRET"
printf '\nStop with: docker compose down\n'
