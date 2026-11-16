#!/usr/bin/env bash
# Backup e restauração do MongoDB. Roda DENTRO do container `backup` do compose (imagem
# mongo:7), para o mesmo comando servir no Windows, macOS e Linux sem bash no host (S10-3):
#
#   docker compose run --rm backup                       → backups/mongo-<data>.archive.gz + retenção
#   docker compose run --rm backup restore <arquivo>     → restaura (sobrescreve as coleções)
#
# Retenção (dias): AUDIO_RETENTION_DAYS=30 (data/audio) e BACKUP_RETENTION_DAYS=30 (backups/).
# O resumo e a transcrição ficam no banco; apagar o áudio antigo só impede reprocessar.
set -euo pipefail

uri="${MONGO_URI:-mongodb://mongo:27017}"
backups="${BACKUP_DIR:-backups}"
audio="${AUDIO_DIR:-data/audio}"

if [ "${1:-}" = "restore" ]; then
  [ -f "${2:-}" ] || { echo "uso: docker compose run --rm backup restore backups/<arquivo>" >&2; exit 1; }
  mongorestore --uri="$uri" --archive="$2" --gzip --drop --quiet
  echo "Restaurado de $2"
  exit 0
fi

mkdir -p "$backups"
out="$backups/mongo-$(date +%Y%m%d-%H%M%S).archive.gz"
mongodump --uri="$uri" --archive="$out" --gzip --quiet
[ -s "$out" ] || { echo "backup vazio: $out" >&2; exit 1; }
echo "Backup: $out ($(du -h "$out" | cut -f1))"

[ -d "$audio" ] && find "$audio" -type f -mtime +"${AUDIO_RETENTION_DAYS:-30}" -delete
find "$backups" -type f -name 'mongo-*.archive.gz' -mtime +"${BACKUP_RETENTION_DAYS:-30}" -delete
