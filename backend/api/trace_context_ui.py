"""One presentation contract for saved context, never infer vector scores or provenance."""

from django.core.paginator import Paginator
from django.template.loader import render_to_string
from django.urls import reverse

from . import access
from .plain_text import clean_context

ERRORS = {
    "context_tokenizer_unavailable": "Для выбранной модели не настроен проверенный подсчёт токенов.",
    "context_invalid_budget": "Окно должно превышать резерв ответа и технический запас.",
    "context_model_metadata_unavailable": "Не удалось проверить ограничения провайдера модели.",
    "context_model_window_unavailable": "Провайдер не поддерживает выбранное окно и резерв ответа.",
    "context_fixed_input_too_large": "Целевое сообщение и обязательные данные не помещаются в окно модели.",
    "context_source_unavailable": "Для сообщения недоступен однозначно определённый чат или проект.",
    "context_processing_in_progress": "Это сообщение уже обрабатывается.",
    "context_configuration_changed": "Настройки модели изменились. Запустите новую попытку анализа.",
    "context_snapshot_mismatch": "Не удалось подтвердить целостность сохранённого запроса.",
    "context_request_uncertain": "Воркер был прерван после отправки запроса. Результат неизвестен; нужна новая попытка.",
    "provider_context_overflow": "Провайдер отклонил размер запроса. История не была скрытно сокращена.",
    "provider_output_truncated": "Ответ модели не поместился в резерв генерации. Факты не сохранены.",
    "reanalysis_access_revoked": "Доступ к повторному анализу был отозван.",
    "provider_request_failed": "Провайдер AI временно недоступен.",
}


def badge_text(obj):
    count = len(obj.earlier_messages_context or [])
    return (
        f"Включено: {count}"
        if obj.context_metadata.get("source") == "chat_history"
        else f"Сохранено: {count}"
    )


def context_view_data(obj, user=None, page=1):
    metadata = obj.context_metadata or {}
    current = (
        metadata.get("source") == "chat_history" and metadata.get("schema_version") == 1
    )
    # New snapshots are already normalized. Re-normalizing would corrupt exact partial ranges.
    items = (
        list(obj.earlier_messages_context or [])
        if current
        else clean_context(obj.earlier_messages_context or [])
    )
    paginator = Paginator(items, 25)
    page_obj = paginator.get_page(page)
    cards = []
    allowed = set()
    if user:
        allowed = set(
            access.messages_for(user)
            .filter(
                pk__in=[
                    m.get("raw_message_id")
                    for m in page_obj
                    if type(m.get("raw_message_id")) is int
                ]
            )
            .values_list("id", flat=True)
        )
    for index, message in enumerate(page_obj, page_obj.start_index()):
        cards.append(
            {
                **message,
                "ordinal": index,
                "content": message.get("content") or message.get("text") or "",
                "sender": message.get("sender_name")
                or message.get("author")
                or "Автор не указан",
                "time": message.get("timestamp")
                or message.get("sent_at")
                or message.get("received_at")
                or "Время неизвестно",
                "source_url": reverse(
                    "admin:api_rawmessage_change", args=[message["raw_message_id"]]
                )
                if message.get("raw_message_id") in allowed
                else None,
            }
        )
    prepared = "input_tokens_preflight" in metadata
    return {
        "trace": obj,
        "context_metadata": metadata,
        "current_context": current,
        "cards": cards,
        "page_obj": page_obj,
        "context_prepared": prepared,
        "context_url": reverse(
            "admin:api_messageprocessingtrace_context", args=[obj.pk]
        ),
        "count_mismatch": obj.earlier_messages_count
        != len(obj.earlier_messages_context or []),
        "context_error": ERRORS.get(
            obj.error_code, "Обработка не завершена; подробности в диагностике."
        )
        if obj.error_code
        else "",
        "context_not_sent": metadata.get("request_state") == "not_sent",
    }


def render_context(obj):
    return render_to_string("admin/message_context.html", context_view_data(obj))
