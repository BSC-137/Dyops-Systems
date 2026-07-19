#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

docker compose up --build -d

printf '\nDyops partner demo is starting:\n'
printf '  UI:       http://localhost:8080\n'
printf '  API docs: http://localhost:8000/docs\n'
printf '  Status:   http://localhost:8000/api/status\n'
printf '\nStop with: docker compose down\n'
