#!/usr/bin/env bash
# Nightly Postgres backup with 7-day retention. Install via cron:
#   0 3 * * * /path/to/sift/deploy/backup.sh >> /var/log/sift-backup.log 2>&1
set -euo pipefail

BACKUP_DIR="${SIFT_BACKUP_DIR:-/var/backups/sift}"
RETENTION_DAYS="${SIFT_BACKUP_RETENTION:-7}"
CONTAINER="${SIFT_PG_CONTAINER:-sift-postgres-1}"

mkdir -p "$BACKUP_DIR"
stamp=$(date +%Y-%m-%d_%H%M%S)
docker compose exec -T postgres pg_dump -U sift -d sift | gzip > "$BACKUP_DIR/sift-$stamp.sql.gz"

find "$BACKUP_DIR" -name 'sift-*.sql.gz' -mtime +"$RETENTION_DAYS" -delete
echo "backup written: $BACKUP_DIR/sift-$stamp.sql.gz"
