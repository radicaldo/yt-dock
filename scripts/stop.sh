#!/usr/bin/env bash
# Stop the yt-dock container. Your transcript library is left untouched.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose down
echo "Container stopped. Your transcript library is safe in ./data/"
