"""Run-owned coverage and bounded history packets; no external calls here."""

from collections import Counter
from django.db import transaction
from django.utils import timezone
from .models import AISettings, HistoryAnalysisItem, OutboxEvent, RawMessage
from .processing_attempts import reserve_attempt
from .dialogue_threads import source_key, analysis_position

POLICY = "history-packets-v1"
ACTIVE = ("queued", "processing", "retry_wait")


@transaction.atomic
def schedule(run):
    cfg = AISettings.get_active()
    limit = run.settings_snapshot.get("analysis_target_message_limit", cfg.analysis_target_message_limit)
    items = []
    for raw in run.messages.order_by("timestamp", "id"):
        item, _ = HistoryAnalysisItem.objects.get_or_create(run=run, raw_message=raw)
        if run.settings_snapshot.get("replace_unsent") and item.outbox_event_id is None:
            from .replay_replacement import guard, LABELS
            code = guard(raw, lock=True)
            if code:
                item.state = "succeeded" if code == "skipped_crm_delivered" else "blocked"
                item.disposition = code if item.state == "succeeded" else "blocked"
                item.reason_code, item.reason_description = code, LABELS[code]
                item.save()
                continue
        if not raw.content.strip():
            item.state, item.disposition = "succeeded", "no_text"
            item.reason_code, item.reason_description = "no_text", "Сообщение не содержит текста для анализа."
            item.save()
        elif item.state == "queued" and item.outbox_event_id is None:
            items.append(item)
    for offset in range(0, len(items), limit):
        packet = items[offset:offset + limit]
        raw = RawMessage.objects.select_for_update().get(pk=packet[0].raw_message_id)
        operation = f"history:{run.id}:packet:{raw.id}"
        traces = {}
        for item in packet:
            trace = reserve_attempt(item.raw_message, f"{operation}:target:{item.raw_message_id}")
            from .processing_attempts import ANALYSIS_CONFIG_FIELDS
            trace.context_metadata.update(analysis_policy=POLICY, analysis_limits={**trace.context_metadata['analysis_limits'], **{
                name: run.settings_snapshot.get(name, getattr(cfg, name))
                for name in ("analysis_input_token_limit", "analysis_output_token_limit", *ANALYSIS_CONFIG_FIELDS)
            }})
            trace.context_metadata['full_history_policy'] = run.settings_snapshot.get('full_history_policy')
            if run.settings_snapshot.get("replace_unsent"):
                trace.context_metadata.update(replace_unsent=True, snapshot_max_id=run.settings_snapshot["snapshot_max_id"])
            trace.save(update_fields=["context_metadata"])
            traces[item.raw_message_id] = trace.id
        event, _ = OutboxEvent.objects.get_or_create(deduplication_key=operation, defaults={
            "event_type": "extract_message", "analysis_source_key": source_key(raw),
            "analysis_position": analysis_position(raw), "payload": {
                "raw_id": raw.id, "trace_id": traces[raw.id],
                "trace_ids": [{"raw_id":pk,"trace_id":trace_id} for pk,trace_id in traces.items()], "batch_ids": [x.raw_message_id for x in packet],
                "history_run_id": run.id, "analysis_policy": POLICY,
            },
        })
        for item in packet:
            item.outbox_event = event
            item.trace_id = traces[item.raw_message_id]
            item.save(update_fields=["outbox_event", "trace", "updated_at"])
        if "root_event_id" not in event.payload:
            event.payload = {**event.payload, "root_event_id":event.id}
            event.save(update_fields=["payload"])
    run.scheduled_count = run.analysis_items.exclude(disposition="no_text").count()
    run.save(update_fields=["scheduled_count"])


def synchronize(event):
    """Reflect durable queue states; succeeded coverage is never reopened."""
    items = HistoryAnalysisItem.objects.filter(outbox_event=event).exclude(state__in=["succeeded", "blocked"])
    state = {"pending": "retry_wait" if event.attempt_count else "queued", "enqueued": "queued",
             "processing": "processing", "cancelled": "cancelled", "failed": "failed",
             "unknown": "failed"}.get(event.state)
    if state:
        from .trace_context_ui import ERRORS
        items.update(state=state, reason_code=event.error_code,
                     reason_description=("Запуск отменён." if state == "cancelled" else
                                         ERRORS.get(event.error_code, f"Анализ не завершён: {event.error_code}.") if event.error_code else ""),
                     updated_at=timezone.now())
    elif event.state == "done" and items.exists():
        # A returned handler without validated per-target coverage is not success.
        items.update(state="failed", reason_code="analysis_coverage_missing",
                     reason_description="Задание завершилось без проверенного результата сообщения.", updated_at=timezone.now())


def counts(run):
    rows = list(run.analysis_items.values("state", "disposition", "reason_code"))
    states = Counter(x["state"] for x in rows)
    outcomes = Counter(x["disposition"] for x in rows if x["state"] == "succeeded")
    result = {"total": len(rows), "processed": states["succeeded"] - outcomes["no_text"] - outcomes["skipped_crm_delivered"],
              "no_text": outcomes["no_text"], "errors": states["failed"],
              "skipped_crm_delivered": outcomes["skipped_crm_delivered"], "blocked":states["blocked"],
              "pending": sum(states[x] for x in ACTIVE), "cancelled": states["cancelled"],
              "facts": outcomes["facts"], "no_facts": outcomes["no_facts"],
              "insufficient_data": outcomes["insufficient_data"],
              "retry_wait": states["retry_wait"], "processing": states["processing"], "queued": states["queued"]}
    events = OutboxEvent.objects.filter(historyanalysisitem__run=run).distinct()
    waiting = events.filter(state="pending", attempt_count__gt=0).order_by("next_attempt_at").first()
    result["next_attempt_at"] = waiting.next_attempt_at.isoformat() if waiting else None
    last_failed = events.filter(state__in=("failed", "unknown")).exclude(error_code="").order_by("-id").first()
    result["last_error"] = waiting.error_code if waiting else last_failed.error_code if last_failed else ""
    from .models import FactCandidate, ProviderUsage
    candidates = FactCandidate.objects.filter(trace_id__in=run.analysis_items.values("trace_id"))
    result["candidates"] = candidates.count()
    result["accepted"] = candidates.filter(status="approved").count()
    result["rejected"] = candidates.filter(status="rejected").count()
    usage = ProviderUsage.objects.filter(outbox_event__in=events, operation="chat", succeeded=True).order_by("-created_at").first()
    result["last_model_response_at"] = usage.created_at.isoformat() if usage else None
    running = events.filter(state="processing").first()
    result["stage"] = "processing" if running else "retry_wait" if waiting else "queued" if result["pending"] else "finished"
    from django.db.models import Max
    useful = run.analysis_items.filter(state="succeeded").exclude(disposition="no_text").aggregate(last=Max("updated_at"))["last"]
    result["last_useful_at"] = useful.isoformat() if useful else None
    return result


def summary(value):
    if "no_text" not in value:
        return f"Обработано {value['processed']} / {value['total']}; ошибок: {value['errors']}"
    accounted = value["processed"] + value["no_text"] + value["errors"] + value["cancelled"] + value["pending"] + value.get("skipped_crm_delivered",0) + value.get("blocked",0)
    return (f"Учтено: {accounted} / {value['total']} · Отменено: {value['cancelled']} · Проанализировано: {value['processed']} / {value['total'] - value['no_text']} · "
            f"Без текста: {value['no_text']} · Уже доставлено в CRM: {value.get('skipped_crm_delivered',0)} · Заблокировано: {value.get('blocked',0)} · С фактами: {value['facts']} · Без фактов: {value['no_facts']} · "
            f"Недостаточно данных: {value['insufficient_data']} · В работе: {value['processing']} · "
            f"В очереди: {value['queued']} · Повторы: {value['retry_wait']} · Ошибки: {value['errors']} · "
            f"Кандидатов: {value['candidates']} · Принято: {value['accepted']}")


@transaction.atomic
def split(event):
    """Transfer ownership to smaller packets without dropping half the targets."""
    items = list(HistoryAnalysisItem.objects.select_for_update().filter(outbox_event=event).exclude(state="succeeded").order_by("raw_message__timestamp", "raw_message_id"))
    if len(items) < 2:
        return False
    from .models import MessageProcessingTrace
    midpoint = (len(items) + 1) // 2
    for index, group in enumerate((items[:midpoint], items[midpoint:])):
        raw = group[0].raw_message
        operation = f"{event.deduplication_key}:split:{index}"
        traces = {}
        for item in group:
            old = item.trace
            trace = reserve_attempt(item.raw_message, f"{operation}:target:{item.raw_message_id}")
            trace.context_metadata.update(analysis_policy=POLICY, analysis_limits=old.context_metadata.get("analysis_limits", {}), parent_trace_id=old.id)
            for field in ("replace_unsent", "snapshot_max_id", "full_history_policy"):
                if field in old.context_metadata:trace.context_metadata[field]=old.context_metadata[field]
            trace.save(update_fields=["context_metadata"])
            traces[item.raw_message_id] = trace.id
        payload = {**event.payload, "raw_id": raw.id, "trace_id": traces[raw.id],
                   "trace_ids": [{"raw_id":pk,"trace_id":trace_id} for pk,trace_id in traces.items()], "batch_ids": [x.raw_message_id for x in group],
                   "repair_generation": event.payload.get("repair_generation", 0) + 1}
        if payload.get('full_history_state'):
            import copy
            from .full_history import merge_results
            preserved = copy.deepcopy(payload['full_history_state'])
            page = preserved.pop('page', None)
            if page and page.get('results'):
                preserved['result'] = merge_results([preserved.get('result'), *page['results']])
                preserved['phase'] = 'reconcile'
            payload['full_history_state'] = preserved
        child, _ = OutboxEvent.objects.get_or_create(deduplication_key=operation, defaults={
            "event_type": "extract_message", "payload": payload,
            "analysis_source_key": event.analysis_source_key, "analysis_position": analysis_position(raw),
        })
        for item in group:
            item.outbox_event = child; item.trace_id = traces[item.raw_message_id]
            item.state, item.reason_code, item.reason_description = "queued", "", ""
            item.save()
    event.state, event.lease_until = "failed", None
    event.save(update_fields=["state", "lease_until"])
    return True
