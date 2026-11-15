#!/usr/bin/env bash
# Backup do MongoDB e limpeza de arquivos antigos (Sprint 3, S3-7).
#
#   scripts/backup.sh                 → gera backups/mongo-<data>.archive.gz e aplica a retenção
#   scripts/backup.sh restore <arq>   → restaura o arquivo (sobrescreve as coleções existentes)
#
# Retenção (dias), ajustável por variável de ambiente:
#   AUDIO_RETENTION_DAYS=30   gravações e arquivos auxiliares em data/audio
#   BACKUP_RETENTION_DAYS=30  arquivos em backups/
# O resumo e a transcrição ficam no banco; apagar o áudio antigo só impede reprocessar.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" = "restore" ]; then
  [ -f "${2:-}" ] || { echo "uso: scripts/backup.sh restore <arquivo>" >&2; exit 1; }
  docker compose exec -T mongo mongorestore --archive --gzip --drop < "$2"
  echo "Restaurado de $2"
  exit 0
fi

mkdir -p backups
out="backups/mongo-$(date +%Y%m%d-%H%M%S).archive.gz"
docker compose exec -T mongo mongodump --archive --gzip --quiet > "$out"
[ -s "$out" ] || { echo "backup vazio: $out" >&2; exit 1; }
echo "Backup: $out ($(du -h "$out" | cut -f1))"

find data/audio -type f -mtime +"${AUDIO_RETENTION_DAYS:-30}" -delete 2>/dev/null || true
find backups -type f -name 'mongo-*.archive.gz' -mtime +"${BACKUP_RETENTION_DAYS:-30}" -delete
