"""Run-owned coverage and bounded history packets; no external calls here."""

from collections import Counter
from django.db import transaction
from django.db.models import Count, Exists, Max, OuterRef, Q, Subquery
from django.utils import timezone
from .models import AISettings, HistoryAnalysisItem, MessageProcessingTrace, OutboxEvent, RawMessage
from .processing_attempts import reserve_attempt
from .dialogue_threads import source_key, analysis_position

POLICY = "history-packets-v1"
ACTIVE = ("queued", "processing", "retry_wait")


@transaction.atomic
def schedule(run):
    cfg = AISettings.get_active()
    from .context_window import LIMIT
    limit = min(LIMIT + 1, run.settings_snapshot.get("analysis_target_message_limit", cfg.analysis_target_message_limit))
    items = []
    raw_rows = list(run.messages.order_by("timestamp", "id"))
    raw_ids = [raw.id for raw in raw_rows]
    snapshot_max_id = run.settings_snapshot.get("snapshot_max_id")
    if snapshot_max_id is None:
        snapshot_max_id = RawMessage.objects.aggregate(last=Max("id"))["last"] or 0
    latest_attempts = dict(MessageProcessingTrace.objects.filter(
        raw_message_id__in=raw_ids
    ).order_by().values("raw_message_id").annotate(last=Max("attempt_no")).values_list(
        "raw_message_id", "last"
    ))
    existing_items = {item.raw_message_id: item for item in HistoryAnalysisItem.objects.filter(
        run=run, raw_message_id__in=raw_ids
    )}
    missing_items = [HistoryAnalysisItem(run=run, raw_message=raw)
                     for raw in raw_rows if raw.id not in existing_items]
    if missing_items:
        HistoryAnalysisItem.objects.bulk_create(missing_items, ignore_conflicts=True, batch_size=500)
        existing_items.update({item.raw_message_id: item for item in HistoryAnalysisItem.objects.filter(
            run=run, raw_message_id__in=[item.raw_message_id for item in missing_items]
        )})
    for raw in raw_rows:
        item = existing_items[raw.id]
        if raw.processing_state == 'clarification_reply':
            item.state, item.disposition = 'succeeded', 'clarification_reply'
            item.reason_code = 'human_clarification_source'
            item.reason_description = 'Ответ на уточнение уже сохранён в человеческом журнале; повторное извлечение моделью не требуется.'
            item.trace = raw.traces.filter(model_version='human-whatsapp-answer').order_by('-id').first()
            item.save()
            continue
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
        operation_keys = [f"{operation}:target:{item.raw_message_id}" for item in packet]
        existing_traces = MessageProcessingTrace.objects.filter(
            operation_key__in=operation_keys
        ).in_bulk(field_name="operation_key")
        traces = {}
        for item in packet:
            operation_key = f"{operation}:target:{item.raw_message_id}"
            previous = existing_traces.get(operation_key)
            trace = reserve_attempt(
                item.raw_message, operation_key,
                cfg=cfg, snapshot_max_id=snapshot_max_id,
                existing=previous,
                latest_attempt_no=latest_attempts.get(item.raw_message_id, 0),
            )
            if previous is None:
                latest_attempts[item.raw_message_id] = trace.attempt_no
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
    run.scheduled_count = run.analysis_items.exclude(disposition__in=("no_text", "clarification_reply")).count()
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


def counts_many(runs):
    """Read live coverage for a bounded set of runs without loading its items."""
    from .models import FactCandidate, ProviderUsage, WhatsAppHistoryRun

    run_ids = [run.pk for run in runs]
    if not run_ids:
        return {}
    states = {pk: Counter() for pk in run_ids}
    outcomes = {pk: Counter() for pk in run_ids}
    useful = {}
    groups = HistoryAnalysisItem.objects.filter(run_id__in=run_ids).order_by().values(
        "run_id", "state", "disposition",
    ).annotate(total=Count("id"), last=Max("updated_at"))
    for group in groups:
        pk = group["run_id"]
        states[pk][group["state"]] += group["total"]
        if group["state"] == "succeeded":
            outcomes[pk][group["disposition"]] += group["total"]
            if group["disposition"] != "no_text" and group["last"] is not None:
                useful[pk] = max(useful.get(pk, group["last"]), group["last"])

    # A trace/event can cover several targets: count each candidate once per run.
    candidate_counts = {
        row["trace__historyanalysisitem__run_id"]: row
        for row in FactCandidate.objects.filter(trace__historyanalysisitem__run_id__in=run_ids)
        .order_by().values("trace__historyanalysisitem__run_id").annotate(
            candidates=Count("id", distinct=True),
            accepted=Count("id", filter=Q(status="approved"), distinct=True),
            rejected=Count("id", filter=Q(status="rejected"), distinct=True),
        )
    }
    events = OutboxEvent.objects.filter(historyanalysisitem__run_id=OuterRef("pk"))
    waiting = events.filter(state="pending", attempt_count__gt=0).order_by("next_attempt_at", "pk")
    failed = events.filter(state__in=("failed", "unknown")).exclude(error_code="").order_by("-id")
    usage = ProviderUsage.objects.filter(
        outbox_event__historyanalysisitem__run_id=OuterRef("pk"), operation="chat", succeeded=True,
    ).order_by("-created_at")
    details = WhatsAppHistoryRun.objects.filter(pk__in=run_ids).order_by().annotate(
        progress_waiting=Exists(waiting),
        progress_next_attempt=Subquery(waiting.values("next_attempt_at")[:1]),
        progress_waiting_error=Subquery(waiting.values("error_code")[:1]),
        progress_failed_error=Subquery(failed.values("error_code")[:1]),
        progress_response_at=Subquery(usage.values("created_at")[:1]),
        progress_running=Exists(events.filter(state="processing")),
    ).values("pk", "progress_waiting", "progress_next_attempt", "progress_waiting_error",
             "progress_failed_error", "progress_response_at", "progress_running")
    result = {}
    for row in details:
        pk = row["pk"]
        state, outcome = states[pk], outcomes[pk]
        value = {"total": sum(state.values()),
                 "processed": state["succeeded"] - outcome["no_text"] - outcome["skipped_crm_delivered"],
                 "no_text": outcome["no_text"], "errors": state["failed"],
                 "skipped_crm_delivered": outcome["skipped_crm_delivered"], "blocked": state["blocked"],
                 "pending": sum(state[name] for name in ACTIVE), "cancelled": state["cancelled"],
                 "facts": outcome["facts"], "no_facts": outcome["no_facts"],
                 "human_answers": outcome["clarification_reply"], "insufficient_data": outcome["insufficient_data"],
                 "retry_wait": state["retry_wait"], "processing": state["processing"], "queued": state["queued"]}
        value.update({key: candidate_counts.get(pk, {}).get(key, 0) for key in ("candidates", "accepted", "rejected")})
        value["next_attempt_at"] = row["progress_next_attempt"].isoformat() if row["progress_next_attempt"] else None
        value["last_error"] = row["progress_waiting_error"] if row["progress_waiting"] else row["progress_failed_error"] or ""
        value["last_model_response_at"] = row["progress_response_at"].isoformat() if row["progress_response_at"] else None
        value["stage"] = "processing" if row["progress_running"] else "retry_wait" if row["progress_waiting"] else "queued" if value["pending"] else "finished"
        value["last_useful_at"] = useful[pk].isoformat() if pk in useful else None
        result[pk] = value
    return result


def counts(run):
    return counts_many([run])[run.pk]


def summary(value):
    if "no_text" not in value:
        return f"Обработано {value['processed']} / {value['total']}; ошибок: {value['errors']}"
    accounted = value["processed"] + value["no_text"] + value["errors"] + value["cancelled"] + value["pending"] + value.get("skipped_crm_delivered",0) + value.get("blocked",0)
    return (f"Учтено: {accounted} / {value['total']} · Отменено: {value['cancelled']} · Проанализировано: {value['processed']} / {value['total'] - value['no_text']} · "
            f"Без текста: {value['no_text']} · Ответов на уточнения: {value.get('human_answers',0)} · Уже доставлено в CRM: {value.get('skipped_crm_delivered',0)} · Заблокировано: {value.get('blocked',0)} · С фактами: {value['facts']} · Без фактов: {value['no_facts']} · "
            f"Недостаточно данных: {value['insufficient_data']} · В работе: {value['processing']} · "
            f"В очереди: {value['queued']} · Повторы: {value['retry_wait']} · Ошибки: {value['errors']} · "
            f"Кандидатов: {value['candidates']} · Принято: {value['accepted']}")


@transaction.atomic
def split(event):
    """Transfer ownership to smaller packets without dropping half the targets."""
    import copy
    items = list(HistoryAnalysisItem.objects.select_for_update().filter(outbox_event=event).exclude(state="succeeded").order_by("raw_message__timestamp", "raw_message_id"))
    if len(items) < 2:
        return False
    owner = MessageProcessingTrace.objects.filter(pk=event.payload.get('trace_id')).first()
    operation_window = owner.context_metadata.get('context_window') if owner else None
    if operation_window is not None and not operation_window['include_following']:
        # A live window cannot be rebound to an earlier target with later
        # sources still attached. Live arrivals are processed individually.
        return False
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
            window = operation_window
            if window is not None:
                window = copy.deepcopy(window)
                window.update(anchor_id=raw.id, target_ids=[entry.raw_message_id for entry in group])
                trace.context_metadata['context_window'] = window
            from .free_fallback import policy_metadata
            trace.context_metadata.update(policy_metadata(old.context_metadata))
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
