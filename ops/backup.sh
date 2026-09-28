#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${BACKUP_RECIPIENT:?Set the public age recipient, not a private identity}"
: "${BACKUP_DIRECTORY:?Set an absolute output directory}"
BACKUP_COMPOSE_FILE=${BACKUP_COMPOSE_FILE:-docker-compose.yml}
BACKUP_PROJECT=${BACKUP_PROJECT:-mazory}
BACKUP_DB_USER=${BACKUP_DB_USER:-mazory}
BACKUP_DB_NAME=${BACKUP_DB_NAME:-mazory_db}
BACKUP_TOOLS_IMAGE=${BACKUP_TOOLS_IMAGE:-mazory-backup-tools}
mkdir -p "$BACKUP_DIRECTORY"
backup_stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_prefix="$BACKUP_DIRECTORY/$backup_stamp"
[[ ! -e "$backup_prefix.database.age" ]] || { echo 'Backup already exists' >&2; exit 1; }
docker compose -f "$BACKUP_COMPOSE_FILE" -p "$BACKUP_PROJECT" exec -T postgres pg_dump -U "$BACKUP_DB_USER" -d "$BACKUP_DB_NAME" -Fc |
  docker run --rm -i "$BACKUP_TOOLS_IMAGE" age -r "$BACKUP_RECIPIENT" > "$backup_prefix.database.age.tmp"
mv "$backup_prefix.database.age.tmp" "$backup_prefix.database.age"
docker compose -f "$BACKUP_COMPOSE_FILE" -p "$BACKUP_PROJECT" exec -T backend tar -C /app/media -cf - . |
  docker run --rm -i "$BACKUP_TOOLS_IMAGE" age -r "$BACKUP_RECIPIENT" > "$backup_prefix.media.age.tmp"
mv "$backup_prefix.media.age.tmp" "$backup_prefix.media.age"
python3 - "$backup_prefix" <<'PY'
import hashlib,json,sys,datetime
from pathlib import Path
prefix=sys.argv[1]
files={}
for kind in ('database','media'):
    path=Path(f'{prefix}.{kind}.age')
    digest=hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda:file.read(1024*1024),b''):
            digest.update(chunk)
    files[kind]={'name':path.name,'sha256':digest.hexdigest(),'bytes':path.stat().st_size}
Path(prefix+'.manifest.json').write_text(json.dumps({'completed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'files':files},indent=2))
print(prefix)
PY
