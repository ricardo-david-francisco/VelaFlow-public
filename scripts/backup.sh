#!/usr/bin/env bash
# =============================================================================
# VelaFlow — Automated Backup Script
# Backs up n8n database + config. Run via cron daily at 3:00 AM.
# Retention: 30 days
# =============================================================================
set -euo pipefail

BACKUP_DIR="/opt/velaflow/backups"
N8N_DATA="/var/lib/docker/volumes/velaflow_n8n_data/_data"
PROJECT_DIR="/opt/velaflow"
RETENTION_DAYS=30
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p "${BACKUP_DIR}"

echo "[backup] Starting backup — ${DATE}"

# Backup n8n SQLite database (online backup, safe while running)
N8N_DB="${N8N_DATA}/database.sqlite"
if [ -f "${N8N_DB}" ]; then
    sqlite3 "${N8N_DB}" ".backup '${BACKUP_DIR}/n8n-db-${DATE}.sqlite'"
    echo "[backup] n8n database backed up."
else
    echo "[backup] WARNING: n8n database not found at ${N8N_DB}"
fi

# Backup config (exclude secrets from tar name, but include the file)
if [ -f "${PROJECT_DIR}/config/.env" ]; then
    cp "${PROJECT_DIR}/config/.env" "${BACKUP_DIR}/env-${DATE}.bak"
    echo "[backup] config/.env backed up."
fi

# Backup Google OAuth token if exists
if [ -f "${PROJECT_DIR}/.google-token.json" ]; then
    cp "${PROJECT_DIR}/.google-token.json" "${BACKUP_DIR}/google-token-${DATE}.json"
    echo "[backup] Google OAuth token backed up."
fi

# Compress old backups
find "${BACKUP_DIR}" -name "n8n-db-*.sqlite" -mtime +1 ! -name "*.gz" \
    -exec gzip {} \; 2>/dev/null || true

# Cleanup old backups
DELETED=$(find "${BACKUP_DIR}" -type f -mtime +${RETENTION_DAYS} -delete -print | wc -l)
if [ "${DELETED}" -gt 0 ]; then
    echo "[backup] Cleaned up ${DELETED} old backup files."
fi

echo "[backup] Done. Backups at: ${BACKUP_DIR}"
