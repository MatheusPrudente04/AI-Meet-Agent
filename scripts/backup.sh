#!/usr/bin/env bash
# Atalho para quem tem bash (macOS, Linux, WSL2). O backup roda no container `backup`:
#   scripts/backup.sh                 = docker compose run --rm backup
#   scripts/backup.sh restore <arq>   = docker compose run --rm backup restore <arq>
# Detalhes e retenção: scripts/mongo_backup.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose run --rm backup "$@"
