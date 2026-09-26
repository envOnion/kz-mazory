import logging
from rest_framework.views import APIView
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework import status
import re
from django_q.tasks import async_task
from django.utils import timezone
from django.conf import settings
from django.db.models import Q, Sum, Count
from .datamart import datamart
from .qdrant_service import qdrant_service
from .ai_service import AIService
from .models import Project, WhatsAppConfig, BitrixSettings
from .serializers import ProjectSerializer

logger = logging.getLogger(__name__)

class KpiSummaryView(APIView):
    """
    Возвращает актуальные показатели KPI команды продаж из детерминированной витрины данных (Data Mart).
    """
    permission_classes = [IsAuthenticated]

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
    permission_classes = [IsAuthenticated]

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
            # 1. Поиск по векторной базе Qdrant (цитаты и сообщения из чатов)
            search_results = qdrant_service.search(prompt, limit=5)
            quotes = [
                f"«{r['payload'].get('content')}» ({r['payload'].get('sender_name', 'Чат')})"
                for r in search_results if r.get('payload')
            ]

            # 2. Финансовые агрегаты по всему портфелю
            agg = Project.objects.aggregate(
                total_count=Count('id'),
                total_amount=Sum('contract_amount'),
                total_paid=Sum('paid_amount'),
                total_due=Sum('due_amount')
            )
            total_count = agg['total_count'] or 0
            total_amount = float(agg['total_amount'] or 0)
            total_paid = float(agg['total_paid'] or 0)
            total_due = float(agg['total_due'] or 0)

            # Статистика по стадиям воронки
            stages_summary = []
            for stage_code, stage_label in Project.STATUS_CHOICES:
                stage_qs = Project.objects.filter(status=stage_code)
                st_count = stage_qs.count()
                if st_count > 0:
                    st_vol = float(stage_qs.aggregate(s=Sum('contract_amount'))['s'] or 0)
                    stages_summary.append({
                        "stage_code": stage_code,
                        "stage_name": stage_label,
                        "count": st_count,
                        "total_amount": st_vol,
                        "formatted_amount": f"{st_vol:,.0f} ₸".replace(',', ' ')
                    })

            portfolio_summary = {
                "total_projects_count": total_count,
                "total_contract_amount": total_amount,
                "total_contract_amount_formatted": f"{total_amount:,.0f} ₸".replace(',', ' '),
                "total_paid_amount": total_paid,
                "total_paid_amount_formatted": f"{total_paid:,.0f} ₸".replace(',', ' '),
                "total_due_amount": total_due,
                "total_due_amount_formatted": f"{total_due:,.0f} ₸".replace(',', ' '),
                "stages_breakdown": stages_summary
            }

            # 3. Интеллектуальный поиск конкретных проектов по токенам запроса
            stop_words = {
                "по", "в", "во", "на", "с", "со", "и", "или", "не", "для", "к", "ко", "до",
                "от", "из", "о", "об", "обо", "за", "под", "при", "про", "что", "как", "где",
                "когда", "кто", "все", "всех", "всем", "всему", "всего", "всеми", "посчитай",
                "покажи", "выведи", "найди", "скажи", "какая", "какой", "какие", "какова",
                "каком", "сколько", "сумма", "сумму", "сумме", "суммы", "договор", "договора",
                "договоров", "договорам", "договорами", "проект", "проекта", "проекты", "проектов",
                "проектам", "объект", "объекта", "объекты", "объектов", "объектам", "деньги",
                "денег", "деньгам", "расчет", "рассчитай", "итог", "итого", "итоговая", "итоговую",
                "стадия", "стадии", "стадиях", "статус", "статусы", "статусах", "компания",
                "компании", "компаний", "клиент", "клиента", "клиенты", "клиентов", "менеджер",
                "менеджера", "менеджеры", "менеджеров", "пожалуйста", "подскажи"
            }

            raw_tokens = [re.sub(r'[^\w\-]', '', w).lower() for w in prompt.split()]
            meaningful_tokens = [t for t in raw_tokens if len(t) >= 3 and t not in stop_words]

            matched_projects_qs = Project.objects.none()
            if meaningful_tokens:
                token_query = Q()
                for t in meaningful_tokens:
                    token_query |= Q(name__icontains=t) | Q(company__name__icontains=t)
                matched_projects_qs = Project.objects.filter(token_query).select_related('company', 'manager').distinct()

            matched_projects = list(matched_projects_qs[:10])

            # Проверяем, носит ли запрос обобщенный/аналитический характер
            is_general_analytical = (
                len(matched_projects) == 0 or
                any(w in prompt_lower for w in [
                    "сумм", "договор", "итог", "всего", "денег", "общ", "статистик",
                    "стади", "марж", "скольк", "ворон", "портфел", "сводк", "план"
                ])
            )

            # Если объект конкретно не найден, или если запрос аналитический — передаем активный реестр
            if not matched_projects or is_general_analytical:
                projects_for_context = list(
                    Project.objects.all().select_related('company', 'manager').order_by('-contract_amount')[:30]
                )
            else:
                projects_for_context = matched_projects

            context_data = {
                "user_prompt": prompt,
                "portfolio_summary": portfolio_summary,
                "matched_projects": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "company": p.company.name if p.company else "Не указано",
                        "manager": p.manager.full_name if p.manager else "Не закреплен",
                        "status": p.get_status_display(),
                        "status_code": p.status,
                        "contract_amount": float(p.contract_amount),
                        "contract_formatted": f"{float(p.contract_amount):,.0f} ₸".replace(',', ' '),
                        "paid_amount": float(p.paid_amount),
                        "paid_formatted": f"{float(p.paid_amount):,.0f} ₸".replace(',', ' '),
                        "due_amount": float(p.due_amount),
                        "due_formatted": f"{float(p.due_amount):,.0f} ₸".replace(',', ' '),
                        "margin": float(p.actual_margin_percent or p.target_margin_percent),
                        "equipment": p.equipment_type,
                        "current_action": p.current_action,
                        "next_action": p.next_action,
                    }
                    for p in projects_for_context
                ],
                "whatsapp_chat_evidence": quotes
            }

            ai_text = AIService.chat_assistant(prompt, context_data)

            # Виджет таблицы отдаем, если есть проекты
            has_projects = len(projects_for_context) > 0
            widget = None
            if has_projects:
                widget_title = (
                    "Связанные объекты и проекты"
                    if (len(matched_projects) > 0 and not is_general_analytical)
                    else "Воронка проектов и контроль маржи"
                )
                widget = {
                    "type": "project_table",
                    "preset": "deal_pipeline",
                    "title": widget_title,
                    "data": datamart.get_pipeline_mart()
                }

            return Response({
                "prompt": prompt,
                "text": ai_text,
                "quotes": quotes,
                "widget": widget
            })


class MessageIngestView(APIView):
    """
    Прием входящих сообщений WhatsApp (от WAHA Webhook или внешних вызовов):
    1. Проверяет авторизационный ключ WAHA API Key.
    2. Распаковывает payload WAHA ({ event: 'message', payload: { ... } }) или плоский JSON.
    3. Фильтрует входящие сообщения по WhatsAppConfig.group_jid (если мониторинг группы настроен).
    4. Ставит событие в очередь Django Q2 на векторизацию, RAG и извлечение сделок.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        # Валидация подлинности вызова вебхука WAHA
        api_key = (
            request.headers.get('X-Api-Key') or
            request.query_params.get('token') or
            request.query_params.get('api_key') or
            (request.headers.get('Authorization', '').split('Bearer ')[-1].strip() if 'Bearer ' in request.headers.get('Authorization', '') else '')
        )
        expected_key = getattr(settings, 'WAHA_API_KEY', '')
        if expected_key and api_key != expected_key:
            return Response(
                {"error": "Unauthorized: invalid or missing WAHA API key"},
                status=status.HTTP_401_UNAUTHORIZED
            )

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
    permission_classes = [IsAuthenticated]
    queryset = Project.objects.all().select_related('company', 'manager').order_by('-contract_amount')
    serializer_class = ProjectSerializer


class BitrixWebhookView(APIView):
    """
    Входящий вебхук от Bitrix24 CRM (события ONCRMDEALADD, ONCRMDEALUPDATE):
    1. Проверяет секретный application token (если задан в BitrixSettings).
    2. Немедленно возвращает HTTP 200 OK (без задержек для Bitrix24).
    3. Передает задачу импорта/обновления сделки в персистентную очередь Redis воркера Django Q2.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        cfg = BitrixSettings.get_active()
        expected_token = (cfg.inbound_token.strip() if cfg and cfg.inbound_token else '') or getattr(settings, 'BITRIX_INBOUND_TOKEN', '')
        if expected_token:
            incoming_token = (
                request.data.get('auth[application_token]') or
                (request.data.get('auth', {}).get('application_token') if isinstance(request.data.get('auth'), dict) else None) or
                request.headers.get('X-Bitrix-Token') or
                request.query_params.get('token')
            )
            if not incoming_token or incoming_token != expected_token:
                return Response(
                    {"error": "Forbidden: invalid Bitrix webhook application token"},
                    status=status.HTTP_403_FORBIDDEN
                )

        event = request.data.get('event') or request.query_params.get('event', '')
        deal_id = (
            request.data.get('data[FIELDS][ID]') or
            (request.data.get('data', {}).get('FIELDS', {}).get('ID') if isinstance(request.data.get('data'), dict) else None) or
            request.data.get('id') or
            request.query_params.get('id')
        )

        if not deal_id:
            return Response({"status": "ignored", "reason": "no_deal_id"}, status=status.HTTP_200_OK)

        task_id = async_task('api.tasks.import_single_deal_from_bitrix_task', str(deal_id))
        return Response({
            "status": "queued",
            "event": event,
            "deal_id": str(deal_id),
            "task_id": task_id
        }, status=status.HTTP_200_OK)

