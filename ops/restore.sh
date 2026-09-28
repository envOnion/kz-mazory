#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${RESTORE_IDENTITY_FILE:?Set the offline age identity file}"
: "${RESTORE_PREFIX:?Set backup path without .database.age suffix}"
: "${RESTORE_DATABASE:?Use a new database beginning with mazory_restore_}"
[[ "$RESTORE_DATABASE" =~ ^mazory_restore_[a-zA-Z0-9_]+$ ]] || { echo 'Restore only targets a new isolated database' >&2; exit 1; }
BACKUP_COMPOSE_FILE=${BACKUP_COMPOSE_FILE:-docker-compose.yml}
BACKUP_PROJECT=${BACKUP_PROJECT:-mazory}
BACKUP_DB_USER=${BACKUP_DB_USER:-mazory}
BACKUP_TOOLS_IMAGE=${BACKUP_TOOLS_IMAGE:-mazory-backup-tools}
restore_container="mazory-restore-$$"
cleanup() { docker rm -f "$restore_container" >/dev/null 2>&1 || true; }
trap cleanup EXIT
python3 - "$RESTORE_PREFIX" <<'PY'
import hashlib,json,sys
from pathlib import Path
prefix=Path(sys.argv[1]); manifest=json.loads(Path(str(prefix)+'.manifest.json').read_text())
for kind in ('database','media'):
    path=Path(str(prefix)+f'.{kind}.age'); digest=hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda:file.read(1024*1024),b''): digest.update(chunk)
    if digest.hexdigest()!=manifest['files'][kind]['sha256']: raise SystemExit('Backup checksum mismatch')
PY
docker create --name "$restore_container" "$BACKUP_TOOLS_IMAGE" sleep 3600 >/dev/null
docker cp "$RESTORE_IDENTITY_FILE" "$restore_container:/identity"
docker cp "$RESTORE_PREFIX.database.age" "$restore_container:/database.age"
docker cp "$RESTORE_PREFIX.media.age" "$restore_container:/media.age"
docker start "$restore_container" >/dev/null
docker compose -f "$BACKUP_COMPOSE_FILE" -p "$BACKUP_PROJECT" exec -T postgres createdb -U "$BACKUP_DB_USER" "$RESTORE_DATABASE"
docker exec "$restore_container" age -d -i /identity /database.age |
  docker compose -f "$BACKUP_COMPOSE_FILE" -p "$BACKUP_PROJECT" exec -T postgres pg_restore -U "$BACKUP_DB_USER" -d "$RESTORE_DATABASE" --no-owner --no-acl --exit-on-error
docker volume create "${RESTORE_DATABASE}_media" >/dev/null
docker exec "$restore_container" age -d -i /identity /media.age |
  docker run --rm -i -v "${RESTORE_DATABASE}_media:/restore" alpine tar -xf - -C /restore
printf 'Restored into isolated database %s and volume %s_media\n' "$RESTORE_DATABASE" "$RESTORE_DATABASE"
