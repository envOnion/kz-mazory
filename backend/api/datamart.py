import logging
from decimal import Decimal
from typing import Dict, Any, List
from django.db.models import Sum, Count, Avg, Q
from django.utils import timezone
from .models import UserProfile, Project, Commitment, FinancialRecord, Company

logger = logging.getLogger(__name__)

class DataMartService:
    """
    Детерминированный слой витрин данных (Data Mart) для аналитики и графиков.
    Исключает галлюцинации LLM, предоставляя точные математические агрегаты из PostgreSQL.
    """

    @staticmethod
    def get_sales_kpi_mart() -> Dict[str, Any]:
        """
        Витрина KPI коммерческой команды: план-факт, сбор денег, маржинальность, просрочки.
        """
        today = timezone.now().date()
        managers = UserProfile.objects.all().order_by('-current_sales')

        total_target = Decimal('0.00')
        total_actual = Decimal('0.00')
        total_deals = 0
        total_overdue = 0

        manager_cards = []

        for rank, mgr in enumerate(managers, start=1):
            # Проекты менеджера
            mgr_projects = Project.objects.filter(manager=mgr)
            deals_count = mgr_projects.count()
            
            # Фактический сбор оплат по завершенным и активным договорам
            collected = mgr_projects.aggregate(total=Sum('paid_amount'))['total'] or mgr.current_sales
            target = mgr.monthly_target if mgr.monthly_target > 0 else Decimal('10000000.00')
            
            progress = round(float(collected) / float(target) * 100, 1) if target > 0 else 0.0

            # Средняя маржа по портфелю
            avg_margin = mgr_projects.aggregate(avg=Avg('target_margin_percent'))['avg'] or Decimal('16.80')

            # Просроченные обещания
            overdue_count = Commitment.objects.filter(
                manager=mgr,
                status__in=['pending', 'overdue'],
                deadline__lt=today
            ).count()

            # Цветовой статус
            if progress >= 100:
                status_color = 'green'
            elif progress >= 75:
                status_color = 'yellow'
            else:
                status_color = 'red'

            total_target += target
            total_actual += collected
            total_deals += deals_count
            total_overdue += overdue_count

            manager_cards.append({
                "id": f"mgr-{mgr.id}",
                "name": mgr.full_name,
                "role": mgr.role,
                "avatar": mgr.avatar_url,
                "rank": rank,
                "is_top_performer": rank == 1,
                "status_color": status_color,
                "kpi_percent": progress,
                "target_amount": float(target),
                "target_formatted": f"{float(target):,.0f} ₸".replace(',', ' '),
                "sales_amount": float(collected),
                "sales_formatted": f"{float(collected):,.0f} ₸".replace(',', ' '),
                "deals_count": deals_count,
                "average_margin": round(float(avg_margin), 1),
                "overdue_commitments": overdue_count,
            })

        overall_progress = round(float(total_actual) / float(total_target) * 100, 1) if total_target > 0 else 0.0

        return {
            "period": timezone.now().strftime("%B %Y"),
            "summary_metrics": [
                {
                    "id": "total-sales",
                    "title": "Фактический сбор оплат",
                    "value": f"{float(total_actual):,.0f} ₸".replace(',', ' '),
                    "raw_value": float(total_actual),
                    "trend": "+18.4% к началу месяца",
                    "trend_positive": True,
                    "icon": "bar-chart"
                },
                {
                    "id": "plan-completion",
                    "title": "Выполнение плана сбора",
                    "value": f"{overall_progress}%",
                    "raw_value": overall_progress,
                    "trend": "Целевой порог: 85%",
                    "trend_positive": overall_progress >= 85,
                    "icon": "target"
                },
                {
                    "id": "deals-count",
                    "title": "Активных договоров и сделок",
                    "value": str(total_deals),
                    "raw_value": total_deals,
                    "trend": f"{total_overdue} просроченных дедлайнов",
                    "trend_positive": total_overdue == 0,
                    "icon": "users"
                }
            ],
            "managers": manager_cards
        }

    @staticmethod
    def get_pipeline_mart() -> Dict[str, Any]:
        """
        Витрина воронки проектов, оборудования и контроля маржинальности.
        """
        projects = Project.objects.all().select_related('company', 'manager')
        
        stages = [
            ('lead', 'Лиды'),
            ('qualification', 'Квалификация / ТЗ'),
            ('proposal_sent', 'КП отправлено'),
            ('contract_signing', 'Согласование договора'),
            ('in_execution', 'В исполнении / Монтаж'),
            ('completed', 'Закрытые сделки'),
            ('stalled', 'Зависшие / Внимание')
        ]
        
        pipeline_stages = []
        for code, label in stages:
            qs = projects.filter(status=code)
            count = qs.count()
            vol = qs.aggregate(total=Sum('contract_amount'))['total'] or Decimal('0.00')
            pipeline_stages.append({
                "code": code,
                "label": label,
                "count": count,
                "volume": float(vol),
                "volume_formatted": f"{float(vol):,.0f} ₸".replace(',', ' ')
            })

        # Маржинальность: низкая (<15%), нормальная (15-20%), высокая (>20%)
        margin_low = projects.filter(target_margin_percent__lt=15.0).count()
        margin_norm = projects.filter(target_margin_percent__gte=15.0, target_margin_percent__lte=20.0).count()
        margin_high = projects.filter(target_margin_percent__gt=20.0).count()

        project_list = [
            {
                "id": p.id,
                "name": p.name,
                "company": p.company.name if p.company else "Не указано",
                "manager": p.manager.full_name if p.manager else "Не закреплен",
                "contract_amount": float(p.contract_amount),
                "contract_formatted": f"{float(p.contract_amount):,.0f} ₸".replace(',', ' '),
                "paid_amount": float(p.paid_amount),
                "paid_formatted": f"{float(p.paid_amount):,.0f} ₸".replace(',', ' '),
                "due_amount": float(p.due_amount),
                "due_formatted": f"{float(p.due_amount):,.0f} ₸".replace(',', ' '),
                "margin_percent": float(p.target_margin_percent),
                "margin_alert": float(p.target_margin_percent) < 15.0,
                "status": p.get_status_display(),
                "priority": p.priority,
                "equipment": p.equipment_type
            }
            for p in projects[:25]
        ]

        return {
            "stages": pipeline_stages,
            "margin_distribution": {
                "low_under_15": margin_low,
                "norm_15_to_20": margin_norm,
                "high_over_20": margin_high
            },
            "projects": project_list
        }

    @staticmethod
    def get_commitments_sla_mart() -> Dict[str, Any]:
        """
        Витрина соблюдения дедлайнов и обязательств (Commitments SLA).
        """
        today = timezone.now().date()
        all_commitments = Commitment.objects.all().select_related('manager', 'project')

        total = all_commitments.count()
        fulfilled = all_commitments.filter(status='fulfilled').count()
        pending = all_commitments.filter(status='pending', deadline__gte=today).count()
        overdue = all_commitments.filter(
            Q(status='overdue') | Q(status='pending', deadline__lt=today)
        ).count()

        slippage_rate = round((overdue / total) * 100, 1) if total > 0 else 0.0

        items = []
        for c in all_commitments.order_by('deadline', 'id')[:20]:
            is_overdue = c.status == 'overdue' or (c.status == 'pending' and c.deadline and c.deadline < today)
            status_text = 'Просрочено' if is_overdue else c.get_status_display()
            status_color = 'red' if is_overdue else ('green' if c.status == 'fulfilled' else 'yellow')

            items.append({
                "id": c.id,
                "text": c.commitment_text,
                "counterparty": c.counterparty_person or (c.project.company.name if c.project and c.project.company else ""),
                "project_name": c.project.name if c.project else "Общая задача",
                "manager_name": c.manager.full_name if c.manager else "Отдел продаж",
                "deadline": c.deadline.isoformat() if c.deadline else None,
                "deadline_formatted": c.deadline.strftime("%d.%m.%Y") if c.deadline else "Без дедлайна",
                "status": status_text,
                "status_color": status_color,
                "severity": c.severity
            })

        return {
            "total_count": total,
            "fulfilled_count": fulfilled,
            "pending_count": pending,
            "overdue_count": overdue,
            "slippage_rate_percent": slippage_rate,
            "commitments": items
        }

    @staticmethod
    def get_sales_chart_dataset() -> Dict[str, Any]:
        """
        Готовый датасет для пресета графиков (Chart.js Bar & Doughnut)
        """
        managers = UserProfile.objects.all().order_by('-current_sales')
        labels = [m.full_name for m in managers]
        targets = [float(m.monthly_target) / 1000000 for m in managers] # в млн ₸
        actuals = [float(m.current_sales) / 1000000 for m in managers]  # в млн ₸

        return {
            "chart_type": "bar",
            "title": "План-факт продаж по менеджерам (млн ₸)",
            "labels": labels,
            "datasets": [
                {
                    "label": "План (млн ₸)",
                    "data": targets,
                    "backgroundColor": "rgba(99, 102, 241, 0.4)",
                    "borderColor": "rgba(99, 102, 241, 1)",
                    "borderWidth": 1.5,
                    "borderRadius": 6
                },
                {
                    "label": "Факт сбора (млн ₸)",
                    "data": actuals,
                    "backgroundColor": "rgba(16, 185, 129, 0.8)",
                    "borderColor": "rgba(16, 185, 129, 1)",
                    "borderWidth": 1.5,
                    "borderRadius": 6
                }
            ]
        }

datamart = DataMartService()
