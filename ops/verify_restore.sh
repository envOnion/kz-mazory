#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
export BACKUP_COMPOSE_FILE=compose.e2e.yml BACKUP_PROJECT=mazory-platform-e2e BACKUP_DB_USER=e2e BACKUP_DB_NAME=mazory_platform_e2e
export BACKUP_DIRECTORY
BACKUP_DIRECTORY=$(mktemp -d /tmp/mazory-backup-check.XXXXXX)
export RESTORE_IDENTITY_FILE="$BACKUP_DIRECTORY/identity.agekey"
docker run --rm mazory-backup-tools age-keygen > "$RESTORE_IDENTITY_FILE" 2>/dev/null
export BACKUP_RECIPIENT
BACKUP_RECIPIENT=$(sed -n 's/^# public key: //p' "$RESTORE_IDENTITY_FILE")
export RESTORE_DATABASE="mazory_restore_$(date -u +%Y%m%d%H%M%S)"
export RESTORE_PREFIX
# Stop only disposable workers so comparisons use a stable fixture snapshot.
docker compose -f compose.e2e.yml stop outbox qcluster delivery crm >/dev/null
trap 'docker compose -f compose.e2e.yml start qcluster delivery crm outbox >/dev/null' EXIT
restore_started=$(date +%s)
RESTORE_PREFIX=$(bash ops/backup.sh)
bash ops/restore.sh
# Compare ordered row hashes for facts, access, provenance and file metadata.
for table in api_project api_financialrecord api_projectrevision api_factcandidate api_factevidence api_rawmessage api_teammembership api_chataccess api_clientprojectaccess api_privateattachment api_authsession api_adminmfa api_paymentallocation api_paymentscheduleitem api_salestarget api_providerusage api_commitment api_notification api_notificationdelivery api_outboxevent; do
  query="SELECT md5(string_agg(row_to_json(t)::text, ',' ORDER BY id)) FROM $table t"
  source_hash=$(docker compose -f compose.e2e.yml exec -T postgres psql -U e2e -d mazory_platform_e2e -Atc "$query")
  restored_hash=$(docker compose -f compose.e2e.yml exec -T postgres psql -U e2e -d "$RESTORE_DATABASE" -Atc "$query")
  [[ "$source_hash" == "$restored_hash" ]] || { echo "Mismatch: $table" >&2; exit 1; }
done
source_media=$(docker compose -f compose.e2e.yml exec -T backend sh -c 'cd /app/media && find . -type f -exec sha256sum {} \;' | sort)
restored_media=$(docker run --rm -v "${RESTORE_DATABASE}_media:/restore:ro" alpine sh -c 'cd /restore && find . -type f -exec sha256sum {} \;' | sort)
[[ "$source_media" == "$restored_media" ]] || { echo 'Restored media mismatch' >&2; exit 1; }
# Evidence remains immutable and replay cannot create another payment.
docker compose -f compose.e2e.yml exec -T -e POSTGRES_DB="$RESTORE_DATABASE" backend python manage.py shell -c '
from api.models import OutboxEvent, FinancialRecord
from api.tasks import run_outbox
before=list(FinancialRecord.objects.values_list("id","amount"))
for event in OutboxEvent.objects.filter(event_type="extract_message",state="done"):
    event.state="pending";event.save(update_fields=["state"]);run_outbox(event.id)
assert list(FinancialRecord.objects.values_list("id","amount"))==before
print("Restore hashes and replay invariant: OK")
'
echo "Restore verification elapsed: $(($(date +%s)-restore_started)) seconds"
echo "Synthetic backup and offline identity: $BACKUP_DIRECTORY; restored DB: $RESTORE_DATABASE"
