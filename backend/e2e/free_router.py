"""Browser-driven provider/queue proof using only the isolated local service."""

import json
import os
import time

from django.conf import settings
from django.db import close_old_connections
from django.db.models import Q
from django_q.tasks import async_task


def probe(index):
    from api.ai_service import AIService
    from api.providers import ProviderUnavailable

    try:
        AIService._post("https://localhost/chat/completions", {
            "model":"openrouter/free", "messages":[{"role":"user","content":"{}"}],
        }, (5, 10), api_key="fixture-key")
        status = "ok"
    except ProviderUnavailable as exc:
        status = str(exc)
    temporary = settings.E2E_DIR / f"router-probe-{index}.tmp"
    temporary.write_text(json.dumps({"status": status, "worker_pid": os.getpid()}))
    temporary.replace(settings.E2E_DIR / f"router-probe-{index}.json")


def dispatch():
    from api.models import AISettings, AsyncOperation, FactCandidate, OutboxEvent, ProviderReservation, ProviderUsage, RawMessage, MessageProcessingTrace
    from api.tasks import dispatch_outbox

    directory = settings.E2E_DIR
    burst = False
    while True:
        close_old_connections()
        if (directory / "router-burst").exists() and not burst:
            burst = True
            for index in range(12):
                async_task("e2e.free_router.probe", index)
        ids = RawMessage.objects.filter(message_id__startswith="router-").values_list("id",flat=True)
        traces = list(MessageProcessingTrace.objects.filter(raw_message_id__in=ids).values("id","raw_message_id","status","error_code","context_metadata"))
        trace_ids = [row["id"] for row in traces]
        operations = AsyncOperation.objects.filter(idempotency_key="router-quota-operation")
        events = list(OutboxEvent.objects.filter(Q(payload__trace_id__in=trace_ids) | Q(payload__raw_id__in=list(ids)) | Q(payload__operation_id__in=list(operations.values_list("id", flat=True)))).values("id","event_type","state","error_code","attempt_count","next_attempt_at"))
        proof = {"settings":list(AISettings.objects.values("minute_request_limit","daily_request_limit","chat_model_name","autonomous_enabled")),
            "messages":list(RawMessage.objects.filter(pk__in=ids).values("id","message_id","processed")),
            "traces":traces,"events":events,
            "facts":list(FactCandidate.objects.filter(trace_id__in=trace_ids).values("trace_id","fact_type","proposed_changes")),
            "operations":list(operations.values("id", "status", "error_code")),
            "usage":list(ProviderUsage.objects.values("id","model_name","operation","succeeded","error_code")),
            "reservations":list(ProviderReservation.objects.values("id","created_at","usage_id")),
            "probes":[json.loads(path.read_text()) for path in directory.glob("router-probe-*.json")]}
        tmp = directory / "router-proof.tmp"
        tmp.write_text(json.dumps(proof,default=str)); tmp.replace(directory / "router-proof.json")
        dispatch_outbox()
        time.sleep(.2)
