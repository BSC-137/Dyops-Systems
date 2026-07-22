#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

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
printf '  UI:       http://localhost:8080\n'
printf '  API docs: http://localhost:8000/docs\n'
printf '  Status:   http://localhost:8000/api/status\n'
printf '  Injection secret: %s\n' "$DYOPS_DEMO_SECRET"
printf '\nStop with: docker compose down\n'
