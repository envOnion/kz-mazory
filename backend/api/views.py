import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status
from django_q.tasks import async_task
from django.utils import timezone
from .datamart import datamart
from .qdrant_service import qdrant_service
from .models import Project

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

        # Интент 5: Поиск по контексту / переписке через Qdrant
        else:
            search_results = qdrant_service.search(prompt, limit=3)
            matched_project = Project.objects.filter(name__icontains=prompt).first()
            
            quotes = [
                f"«{r['payload'].get('content')}» ({r['payload'].get('sender_name', 'Чат')})"
                for r in search_results if r.get('payload')
            ]

            summary_text = f"По запросу «{prompt}» найдены следующие данные:"
            if matched_project:
                summary_text += f"\nОбъект: {matched_project.name}, Сумма: {float(matched_project.contract_amount):,.0f} ₸, Статус: {matched_project.get_status_display()}."

            return Response({
                "prompt": prompt,
                "text": summary_text,
                "quotes": quotes,
                "widget": {
                    "type": "project_table",
                    "preset": "deal_pipeline",
                    "title": "Результаты поиска по базе объектов",
                    "data": datamart.get_pipeline_mart()
                }
            })


class MessageIngestView(APIView):
    """
    Прием нового входящего сообщения (через WhatsApp Webhook или вручную)
    и передача события в очередь фонового воркера.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        content = request.data.get('content') or request.data.get('body') or ''
        if not content:
            return Response({"error": "Content is required"}, status=status.HTTP_400_BAD_REQUEST)

        message_data = {
            "message_id": request.data.get('id') or f"msg-{int(timezone.now().timestamp() * 1000)}",
            "content": content,
            "sender_name": request.data.get('sender_name') or request.data.get('from', 'Неизвестный'),
            "sender_phone": request.data.get('sender_phone') or request.data.get('from', ''),
            "chat_id": request.data.get('chat_id', 'aquakip-sales')
        }

        # Отправляем задачу в очередь Django Q2
        task_id = async_task('api.tasks.process_incoming_message_task', message_data)

        return Response({
            "status": "queued",
            "task_id": task_id,
            "message": "Событие 'поступило новое сообщение' отправлено воркеру на обработку"
        }, status=status.HTTP_202_ACCEPTED)
