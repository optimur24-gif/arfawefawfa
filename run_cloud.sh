#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
test -f .env || { echo 'Create .env with BOT_TOKEN first.' >&2; exit 1; }
docker compose up -d --build
docker compose logs --tail=30 bot
