from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

class KpiSummaryView(APIView):
    """
    Returns KPI metrics, manager performance cards and AI insights.
    """
    def get(self, request):
        data = {
            "category_badge": "AI АНАЛИЗ",
            "query_title": "Покажи KPI менеджеров",
            "query_subtitle": "Актуальные показатели по команде продаж",
            "updated_at_text": "Обновлено сегодня в 10:24",
            "summary_metrics": [
                {
                    "id": "total-sales",
                    "title": "Общие продажи",
                    "value": "24 500 000 ₽",
                    "trend": "+12% к прошлому месяцу",
                    "trend_positive": True,
                    "icon": "bar-chart"
                },
                {
                    "id": "plan-completion",
                    "title": "Выполнение плана",
                    "value": "87%",
                    "trend": "+6 п.п. к прошлому месяцу",
                    "trend_positive": True,
                    "icon": "target"
                },
                {
                    "id": "deals-count",
                    "title": "Количество сделок",
                    "value": "142",
                    "trend": "+18% к прошлому месяцу",
                    "trend_positive": True,
                    "icon": "users"
                }
            ],
            "managers": [
                {
                    "id": "m1",
                    "name": "Максим Кузнецов",
                    "role": "Старший менеджер",
                    "avatar": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?auto=format&fit=crop&w=200&q=80",
                    "is_top_performer": True,
                    "status_color": "green",
                    "kpi_percent": 104,
                    "kpi_bar_color": "green",
                    "sales_amount": "7 800 000 ₽",
                    "deals_count": 28,
                    "trend": "+26% к прошлому месяцу",
                    "trend_positive": True
                },
                {
                    "id": "m2",
                    "name": "Ирина Волкова",
                    "role": "Менеджер по продажам",
                    "avatar": "https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?auto=format&fit=crop&w=200&q=80",
                    "status_color": "green",
                    "kpi_percent": 92,
                    "kpi_bar_color": "green",
                    "sales_amount": "5 400 000 ₽",
                    "deals_count": 24,
                    "trend": "+14% к прошлому месяцу",
                    "trend_positive": True
                },
                {
                    "id": "m3",
                    "name": "Даниил Соколов",
                    "role": "Менеджер по продажам",
                    "avatar": "https://images.unsplash.com/photo-1500648767791-00dcc994a43e?auto=format&fit=crop&w=200&q=80",
                    "status_color": "yellow",
                    "kpi_percent": 78,
                    "kpi_bar_color": "yellow",
                    "sales_amount": "4 900 000 ₽",
                    "deals_count": 22,
                    "trend": "+6% к прошлому месяцу",
                    "trend_positive": True
                },
                {
                    "id": "m4",
                    "name": "Алина Смирнова",
                    "role": "Менеджер по продажам",
                    "avatar": "https://images.unsplash.com/photo-1544005313-94ddf0286df2?auto=format&fit=crop&w=200&q=80",
                    "status_color": "red",
                    "kpi_percent": 61,
                    "kpi_bar_color": "red",
                    "sales_amount": "3 200 000 ₽",
                    "deals_count": 14,
                    "trend": "-18% к прошлому месяцу",
                    "trend_positive": False
                },
                {
                    "id": "m5",
                    "name": "Егор Новиков",
                    "role": "Менеджер по продажам",
                    "avatar": "https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?auto=format&fit=crop&w=200&q=80",
                    "status_color": "green",
                    "kpi_percent": 88,
                    "kpi_bar_color": "green",
                    "sales_amount": "3 200 000 ₽",
                    "deals_count": 18,
                    "trend": "+11% к прошлому месяцу",
                    "trend_positive": True
                }
            ],
            "insight": {
                "badge": "AI-инсайт",
                "source": "На основе анализа сделок, активности и конверсий",
                "headline": "Лучший результат у Максима — 104% плана. У Алины — 61%.",
                "details": "Основная причина отставания — снижение конверсии (меньше сделок при том же объеме активности).",
                "actions": [
                    { "id": "why", "label": "Почему?", "icon": "search" },
                    { "id": "deals", "label": "Показать сделки", "icon": "file-text" },
                    { "id": "compare", "label": "Сравнить с прошлым месяцем", "icon": "bar-chart-2" },
                    { "id": "contact", "label": "Написать сотруднику", "icon": "send" }
                ]
            }
        }
        return Response(data)


class ChatQueryView(APIView):
    """
    Handles prompt requests sent to Mazory AI.
    """
    def post(self, request):
        prompt = request.data.get("prompt", "").strip()
        if not prompt:
            return Response({"error": "Prompt cannot be empty"}, status=status.HTTP_400_BAD_REQUEST)

        # For testing / prototyping, returns formatted AI reply
        return Response({
            "prompt": prompt,
            "status": "success",
            "reply": f"Обработан запрос '{prompt}'. Данные актуализированы."
        })
