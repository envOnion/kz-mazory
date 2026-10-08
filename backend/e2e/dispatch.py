"""Local publisher using the genuine Django Q2 ORM broker and a separate worker."""

import time
import json
from django.conf import settings
from django.db import close_old_connections
from api.models import AISettings, OutboxEvent
from django_q.tasks import async_task
from django.utils import timezone


def dispatch_forever():
    analytics_owner_id = json.loads((settings.E2E_DIR / 'analytics-session.json').read_text())['user_id']
    while True:
        close_old_connections()
        control = json.loads((settings.E2E_DIR / "provider-state.json").read_text())
        from api.models import MessageProcessingTrace
        completed = dict(MessageProcessingTrace.objects.filter(raw_message__message_id__startswith='full-', status='success').order_by('id').values_list('raw_message_id','id'))
        temporary = settings.E2E_DIR/'thread-completed.tmp'
        temporary.write_text(json.dumps(completed)); temporary.replace(settings.E2E_DIR/'thread-completed.json')
        if control.get('thread_window'):
            AISettings.objects.update(context_window_tokens=control['thread_window'], analysis_input_token_limit=0,
                                      autonomous_enabled=False, autonomous_crm_enabled=False)
        if control.get('analytics_role'):
            from api.models import TeamMembership
            TeamMembership.objects.filter(user_id=analytics_owner_id).update(role=control['analytics_role'])
        if type(control.get('analytics_operation_expire')) is int:
            from api.models import AsyncOperation
            AsyncOperation.objects.filter(pk=control['analytics_operation_expire'], requested_by_id=analytics_owner_id).update(expires_at=timezone.now())
        if 'analytics_revoke' in control:
            from api.models import TeamMembership, AnalyticsArtifact
            TeamMembership.objects.filter(user_id=analytics_owner_id).update(status='revoked' if control['analytics_revoke'] else 'active')
        if control.get('analytics_expire'):
            from api.models import AnalyticsArtifact
            AnalyticsArtifact.objects.filter(turn__conversation__owner_id=analytics_owner_id).update(expires_at=timezone.now())
        if control.get("autonomous"):
            AISettings.objects.update(autonomous_enabled=True, autonomous_crm_enabled=True)
            OutboxEvent.objects.get_or_create(deduplication_key="e2e-autonomous-reports", defaults={"event_type": "autonomous_reconcile", "payload": {}})
        # Indexing is outside this scenario (no vector database in portable e2e).
        OutboxEvent.objects.filter(event_type="index_message", state="pending").update(
            state="done"
        )
        for event in (
            OutboxEvent.objects.filter(
                state="pending", next_attempt_at__lte=timezone.now()
            )
            .exclude(event_type="index_message")
            .order_by("id")[:100]
        ):
            if event.event_type == 'extract_message' and control.get('pause_thread_after_stage') and len(event.payload.get('full_history_state', {}).get('stage_trace_ids', [])) >= control['pause_thread_after_stage']:
                (settings.E2E_DIR/'thread-paused.json').write_text(json.dumps({'event_id':event.id, 'trace_id':event.payload['trace_id'], 'history_run_id':event.payload.get('history_run_id'), 'state':event.payload['full_history_state']}))
                continue
            if OutboxEvent.objects.filter(pk=event.id, state="pending").update(
                state="enqueued"
            ):
                async_task("api.tasks.run_outbox", event.id)
        time.sleep(0.2)
