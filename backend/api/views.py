import logging
from rest_framework.views import APIView
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status
from django_q.tasks import async_task
from django.utils import timezone
from django.db.models import Q
from .datamart import datamart
from .qdrant_service import qdrant_service
from .ai_service import AIService
from .models import Project, WhatsAppConfig
from .serializers import ProjectSerializer

logger = logging.getLogger(__name__)

class KpiSummaryView(APIView):
    """
    Возвращает актуальные показатели KPI команды продаж из детерминированной витрины данных (Data Mart).
    """
    permission_classes = [AllowAny]

    def get(self, request):
        kpi_data = datamart.get_sales_kpi_mart()
        
        # Топ-перформер и аналитический инсайт
        managers = kpi_data.get("managers", [])
        top_name = managers[0]["name"] if managers else "Жанат Бейсбаев"
        top_val = managers[0]["kpi_percent"] if managers else 99.7

        response_data = {
            "category_badge": "AQUA KIP DATA MART",
            "query_title": "KPI отдела продаж",
            "query_subtitle": f"Показатели за {kpi_data.get('period', 'Текущий месяц')} (в тенге ₸)",
            "updated_at_text": f"Обновлено {timezone.now().strftime('%d.%m.%Y в %H:%M')}",
            "summary_metrics": kpi_data.get("summary_metrics", []),
            "managers": managers,
            "insight": {
                "badge": "AI-инсайт",
                "source": "На основе витрины данных Data Mart (сбор денег, маржа, дедлайны)",
                "headline": f"Лидер по сбору денег — {top_name} ({top_val}% плана).",
                "details": "Ключевой фактор роста — крупные закрытые контракты по ПСЭМ и Top Build. По проектам с маржой ниже 15% требуется особый контроль.",
                "actions": [
                    { "id": "why", "label": "Почему?", "icon": "search" },
                    { "id": "deals", "label": "Показать сделки", "icon": "file-text" },
                    { "id": "chart", "label": "Вывести график продаж", "icon": "bar-chart-2" },
                    { "id": "commitments", "label": "Обещания и дедлайны", "icon": "clock" }
                ]
            }
        }
        return Response(response_data)


class ChatQueryView(APIView):
    """
    Интеллектуальный AI-оркестратор:
    1. Классифицирует намерение (Intent).
    2. Извлекает выверенные данные из Data Mart или первоисточники из Qdrant.
    3. Возвращает ответ и спецификацию UI-виджета для динамического рендеринга на Vue 3.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        prompt = request.data.get("prompt", "").strip()
        if not prompt:
            return Response({"error": "Prompt cannot be empty"}, status=status.HTTP_400_BAD_REQUEST)

        prompt_lower = prompt.lower()

        # Интент 1: График продаж / сравнение плана и факта
        if any(w in prompt_lower for w in ["график", "диаграмм", "сравни", "chart", "бар"]):
            chart_data = datamart.get_sales_chart_dataset()
            return Response({
                "prompt": prompt,
                "text": "Построен сравнительный график выполнения плана продаж и фактического сбора денег по менеджерам.",
                "widget": {
                    "type": "chart",
                    "preset": "bar_sales",
                    "title": chart_data["title"],
                    "data": chart_data
                },
                "insights": [
                    "Жанат и Самат обеспечивают более 75% общего объема сбора оплат компании.",
                    "Улугбек перевыполнил план сбора за счет закрытия платежа 117 млн ₸ от Top Build."
                ]
            })

        # Интент 2: Обещания, дедлайны, напоминания, горящие задачи
        elif any(w in prompt_lower for w in ["обещ", "дедлайн", "напомин", "задач", "сроки", "горит", "просроч"]):
            commitments_mart = datamart.get_commitments_sla_mart()
            overdue_count = commitments_mart["overdue_count"]
            return Response({
                "prompt": prompt,
                "text": f"Сформирован реестр обещаний и обязательств. На контроле {commitments_mart['total_count']} договоренностей, из них просрочено: {overdue_count}.",
                "widget": {
                    "type": "commitments_list",
                    "preset": "commitments_sla",
                    "title": "Реестр обещаний и дедлайнов (SLA контроля)",
                    "data": commitments_mart
                },
                "insights": [
                    "Критично: Квалификация 6 заводов (Coca-Cola, HOWO, VOLTREX) просрочена.",
                    "Выполнено: Все работы по Integra закрыты, оборудование по Top Build отгружено."
                ]
            })

        # Интент 3: Воронка сделок, проекты, объекты
        elif any(w in prompt_lower for w in ["сделк", "объект", "проект", "воронк", "пайплайн", "pipeline"]):
            pipeline_mart = datamart.get_pipeline_mart()
            return Response({
                "prompt": prompt,
                "text": "Актуальная воронка проектов и распределение объектов Aqua Kip по стадиям и маржинальности.",
                "widget": {
                    "type": "project_table",
                    "preset": "deal_pipeline",
                    "title": "Воронка проектов и контроль маржи",
                    "data": pipeline_mart
                },
                "insights": [
                    "Внимание: проекты ПСЭМ 190 и ПСЭМ 100 имеют маржу 14.0–14.4% (порог 15%). Любые допработы требуют визы генерального директора.",
                    "Лучший маржинальный кейс: Алтын Сити (46.3% маржи)."
                ]
            })

        # Интент 4: KPI менеджеров
        elif any(w in prompt_lower for w in ["kpi", "кпи", "план", "команд", "менеджер"]):
            kpi_mart = datamart.get_sales_kpi_mart()
            return Response({
                "prompt": prompt,
                "text": "Сводка выполнения KPI коммерческой команды продаж Aqua Kip Engineering.",
                "widget": {
                    "type": "kpi_grid",
                    "preset": "manager_grid",
                    "title": "KPI отдела продаж",
                    "data": kpi_mart
                },
                "insights": [
                    "Лидер рейтинга — Жанат Бейсбаев (568.27 млн ₸).",
                    "Камиль ведет подписание 10 МВт Vertex Garden (156 млн ₸) и согласование котельной 2 МВт Казына Парк."
                ]
            })

        # Интент 5: Поиск по контексту / переписке через Qdrant + AI Генерация ответа
        else:
            search_results = qdrant_service.search(prompt, limit=5)
            matched_projects = list(Project.objects.filter(
                Q(name__icontains=prompt) | Q(company__name__icontains=prompt)
            )[:5])
            
            quotes = [
                f"«{r['payload'].get('content')}» ({r['payload'].get('sender_name', 'Чат')})"
                for r in search_results if r.get('payload')
            ]

            context_data = {
                "user_prompt": prompt,
                "matched_projects": [
                    {
                        "name": p.name,
                        "company": p.company.name if p.company else None,
                        "status": p.get_status_display(),
                        "amount": float(p.contract_amount),
                        "paid": float(p.paid_amount),
                        "due": float(p.due_amount),
                        "margin": float(p.actual_margin_percent),
                        "current_action": p.current_action,
                        "next_action": p.next_action,
                    }
                    for p in matched_projects
                ],
                "whatsapp_chat_evidence": quotes
            }

            ai_text = AIService.chat_assistant(prompt, context_data)

            return Response({
                "prompt": prompt,
                "text": ai_text,
                "quotes": quotes,
                "widget": {
                    "type": "project_table",
                    "preset": "deal_pipeline",
                    "title": "Связанные объекты и проекты",
                    "data": datamart.get_pipeline_mart()
                } if matched_projects else None
            })


class MessageIngestView(APIView):
    """
    Прием входящих сообщений WhatsApp (от WAHA Webhook или внешних вызовов):
    1. Распаковывает payload WAHA ({ event: 'message', payload: { ... } }) или плоский JSON.
    2. Фильтрует входящие сообщения по WhatsAppConfig.group_jid (если мониторинг группы настроен).
    3. Ставит событие в очередь Django Q2 на векторизацию, RAG и извлечение сделок.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        payload = data.get('payload') if isinstance(data.get('payload'), dict) else data

        # Пропускаем исходящие сообщения от самого бота, если указано fromMe
        if payload.get('fromMe') is True:
            return Response({"status": "ignored", "reason": "outgoing_message"}, status=status.HTTP_200_OK)

        content = (
            payload.get('body')
            or payload.get('content')
            or data.get('content')
            or data.get('body')
            or ''
        ).strip()

        if not content:
            return Response({"error": "Content is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Извлечение параметров чата и отправителя
        chat_id = payload.get('from') or data.get('chat_id') or 'aquakip-sales'
        sender_phone = (
            payload.get('participant')
            or payload.get('author')
            or payload.get('from')
            or data.get('sender_phone')
            or ''
        )
        sender_name = (
            payload.get('_data', {}).get('notifyName')
            or payload.get('notifyName')
            or data.get('sender_name')
            or 'Коллега'
        )
        message_id = (
            payload.get('id')
            or data.get('id')
            or f"msg-{int(timezone.now().timestamp() * 1000)}"
        )

        # Проверка соответствия настроенной группе WhatsApp в БД
        cfg = WhatsAppConfig.get_active()
        if cfg.is_active and cfg.group_jid and chat_id.endswith('@g.us'):
            if chat_id != cfg.group_jid:
                logger.info("Ignoring WAHA message from unmonitored group %s (monitored: %s)", chat_id, cfg.group_jid)
                return Response({
                    "status": "ignored",
                    "reason": f"Chat {chat_id} is not configured in WhatsAppConfig"
                }, status=status.HTTP_200_OK)

        message_data = {
            "message_id": message_id,
            "content": content,
            "sender_name": sender_name,
            "sender_phone": sender_phone,
            "chat_id": chat_id,
            "raw_payload": data
        }

        # Отправляем задачу в очередь фонового воркера Django Q2
        task_id = async_task('api.tasks.process_incoming_message_task', message_data)

        return Response({
            "status": "queued",
            "task_id": task_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "message": "Сообщение успешно поставлено в очередь на AI-обработку"
        }, status=status.HTTP_202_ACCEPTED)


class ProjectListView(ListAPIView):
    """
    Реестр всех объектов и сделок Aqua Kip Engineering.
    """
    permission_classes = [AllowAny]
    queryset = Project.objects.all().select_related('company', 'manager').order_by('-contract_amount')
    serializer_class = ProjectSerializer
