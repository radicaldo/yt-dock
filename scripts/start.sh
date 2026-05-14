#!/usr/bin/env bash
# Start yt-dock in the background. Resolves the host port from
# $YTDOCK_PORT, then .env, then defaults to 8000.
set -euo pipefail
cd "$(dirname "$0")/.."

port="${YTDOCK_PORT:-}"
if [ -z "$port" ] && [ -f .env ]; then
  port="$(grep -E '^[[:space:]]*YTDOCK_PORT[[:space:]]*=' .env | tail -n1 | cut -d= -f2 | tr -d '[:space:]' || true)"
fi
port="${port:-8000}"

docker compose up -d || { echo "docker compose failed - check the output above." >&2; exit 1; }

echo
echo "yt-dock is running."
echo "  UI:        http://localhost:${port}"
echo "  API docs:  http://localhost:${port}/docs"
echo "  Health:    http://localhost:${port}/health"
echo
echo "Data persists in: $(pwd)/data/transcripts.db"
