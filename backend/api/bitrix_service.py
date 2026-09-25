import logging
from decimal import Decimal
from typing import Dict, Any, Optional, List
import requests
from django.utils import timezone
from api.models import BitrixSettings, Project, Company, UserProfile, BusinessEvent
from api.deduplication import normalize_deal_name

logger = logging.getLogger(__name__)

class BitrixService:
    @staticmethod
    def get_webhook_url() -> str:
        cfg = BitrixSettings.get_active()
        return cfg.webhook_url.rstrip('/') + '/'

    @staticmethod
    def call(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = BitrixService.get_webhook_url() + method
        try:
            res = requests.post(url, json=params or {}, timeout=20)
            res.raise_for_status()
            data = res.json()
            if "error" in data:
                raise RuntimeError(f"Bitrix24 API error ({method}): {data.get('error_description') or data['error']}")
            return data
        except Exception as e:
            logger.error("Bitrix24 API call failed for %s: %s", method, e)
            raise

    @staticmethod
    def status_to_stage(status: str) -> str:
        """Маппинг внутренних статусов Mazory в стадии Bitrix24."""
        mapping = {
            'lead': 'NEW',
            'qualification': 'PREPARATION',
            'proposal_sent': 'PREPAYMENT_INVOICE',
            'contract_signing': 'EXECUTING',
            'in_execution': 'FINAL_INVOICE',
            'completed': 'WON',
            'stalled': 'APOLOGY',
            'lost': 'LOSE',
        }
        return mapping.get(status, 'PREPARATION')

    @staticmethod
    def stage_to_status(stage_id: str) -> str:
        """Маппинг стадий Bitrix24 во внутренние статусы Mazory."""
        stage = (stage_id or '').upper()
        if 'WON' in stage or 'FINAL' in stage:
            return 'completed'
        if 'LOSE' in stage or 'APOLOGY' in stage:
            return 'lost'
        if 'EXECUT' in stage or 'PROD' in stage:
            return 'in_execution'
        if 'PREPAY' in stage or 'SIGN' in stage or 'CONTRACT' in stage:
            return 'contract_signing'
        if 'PREPAR' in stage or 'PROPOSAL' in stage:
            return 'proposal_sent'
        if 'QUALIF' in stage or 'NEW' in stage:
            return 'qualification'
        return 'qualification'

    @staticmethod
    def find_deal_by_name(name: str) -> Optional[Dict[str, Any]]:
        """
        Ищет существующую сделку в Bitrix24 CRM по названию перед созданием.
        Использует фильтр по %TITLE% и кастомному полю UF_CRM_1731131779572 (Объекты).
        Возвращает данные первой найденной сделки или None.
        """
        if not name or not name.strip():
            return None

        clean_name = name.strip()
        core_name = normalize_deal_name(clean_name)
        if not core_name:
            core_name = clean_name

        try:
            # 1. Поиск по подстроке в TITLE
            resp = BitrixService.call("crm.deal.list", {
                "filter": {"%TITLE": core_name},
                "select": ["ID", "TITLE", "OPPORTUNITY", "STAGE_ID", "ASSIGNED_BY_ID", "COMPANY_ID",
                           "UF_CRM_1731131779572", "UF_CRM_1778166248543", "UF_CRM_1778164670507", "COMMENTS", "DATE_CREATE"],
                "order": {"ID": "ASC"}
            })
            deals = resp.get("result", [])
            if deals:
                logger.info("Found %d deals in Bitrix24 matching '%s', using #%s", len(deals), core_name, deals[0].get("ID"))
                return deals[0]

            # 2. Если по TITLE не найдено, поиск по кастомному полю объекта
            resp_custom = BitrixService.call("crm.deal.list", {
                "filter": {"%UF_CRM_1731131779572": core_name},
                "select": ["ID", "TITLE", "OPPORTUNITY", "STAGE_ID", "ASSIGNED_BY_ID", "COMPANY_ID",
                           "UF_CRM_1731131779572", "UF_CRM_1778166248543", "UF_CRM_1778164670507", "COMMENTS", "DATE_CREATE"],
                "order": {"ID": "ASC"}
            })
            deals_custom = resp_custom.get("result", [])
            if deals_custom:
                logger.info("Found %d deals in Bitrix24 matching custom field '%s', using #%s", len(deals_custom), core_name, deals_custom[0].get("ID"))
                return deals_custom[0]

            return None
        except Exception as e:
            logger.error("Error searching deal by name '%s' in Bitrix24: %s", name, e)
            return None

    @staticmethod
    def get_deal(deal_id: str) -> Optional[Dict[str, Any]]:
        """Получает полные данные сделки из Bitrix24 по ID."""
        try:
            resp = BitrixService.call("crm.deal.get", {"id": deal_id})
            return resp.get("result")
        except Exception as e:
            logger.error("Failed to fetch deal #%s from Bitrix24: %s", deal_id, e)
            return None

    @staticmethod
    def create_deal(project_data: Dict[str, Any]) -> Optional[str]:
        """
        Создает сделку в Bitrix24 CRM через crm.deal.add.
        Перед созданием ПРОВЕРЯЕТ наличие сделки, чтобы исключить дубли.
        """
        cfg = BitrixSettings.get_active()
        if not cfg.is_active or not cfg.auto_create_deals:
            logger.info("Bitrix deal auto-creation is disabled in BitrixSettings")
            return None

        title = project_data.get("name") or project_data.get("object_name") or "Новая сделка из WhatsApp"
        
        # Защита от дублей: обязательный предварительный поиск в CRM
        existing = BitrixService.find_deal_by_name(title)
        if existing:
            deal_id = str(existing.get("ID"))
            logger.warning("Duplicate prevented: Deal '%s' already exists in Bitrix24 with ID %s. Skipping creation.", title, deal_id)
            return deal_id

        amount = project_data.get("contract_amount") or project_data.get("amount") or 0.0
        stage_id = project_data.get("stage_id") or BitrixService.status_to_stage(project_data.get("status", "qualification"))

        fields = {
            "TITLE": title,
            "OPPORTUNITY": float(amount),
            "CURRENCY_ID": "KZT",
            "STAGE_ID": stage_id,
            "CATEGORY_ID": cfg.deal_category_id,
            "ASSIGNED_BY_ID": project_data.get("assigned_by_id") or cfg.default_assigned_by_id,
            "UF_CRM_1731131779572": title,
            "UF_CRM_1778166248543": project_data.get("direction") or project_data.get("equipment_type") or "",
            "UF_CRM_1778164670507": project_data.get("deal_period") or "",
            "COMMENTS": f"Автоматически создано Mazory AI из WhatsApp чата.<br>Действие: {project_data.get('current_action') or ''}<br>Следующий шаг: {project_data.get('next_action') or ''}"
        }

        try:
            resp = BitrixService.call("crm.deal.add", {"fields": fields})
            deal_id = str(resp.get("result"))
            logger.info("Bitrix deal successfully created with ID: %s", deal_id)
            return deal_id
        except Exception as e:
            logger.error("Failed to create deal in Bitrix: %s", e)
            return None

    @staticmethod
    def update_deal(deal_id: str, fields_to_update: Dict[str, Any]) -> bool:
        """
        Обновляет существующую сделку в Bitrix24 CRM (crm.deal.update).
        """
        try:
            resp = BitrixService.call("crm.deal.update", {
                "id": deal_id,
                "fields": fields_to_update
            })
            ok = bool(resp.get("result"))
            logger.info("Bitrix deal %s updated successfully: %s", deal_id, ok)
            return ok
        except Exception as e:
            logger.error("Failed to update deal %s in Bitrix24: %s", deal_id, e)
            return False

    @staticmethod
    def add_timeline_comment(deal_id: str, comment_text: str) -> Optional[str]:
        """
        Добавляет структурированную запись в таймлайн сделки (crm.timeline.comment.add).
        """
        try:
            resp = BitrixService.call("crm.timeline.comment.add", {
                "fields": {
                    "ENTITY_ID": deal_id,
                    "ENTITY_TYPE": "deal",
                    "COMMENT": comment_text
                }
            })
            comment_id = str(resp.get("result"))
            logger.info("Timeline comment added to deal %s (ID: %s)", deal_id, comment_id)
            return comment_id
        except Exception as e:
            logger.error("Failed to add timeline comment to deal %s: %s", deal_id, e)
            return None

    @staticmethod
    def create_task(title: str, description: str, deadline_iso: Optional[str] = None,
                    responsible_id: Optional[int] = None, deal_id: Optional[str] = None) -> Optional[str]:
        """
        Создает задачу в модуле 'Задачи и Проекты' Bitrix24 (tasks.task.add)
        с привязкой к сделке через UF_CRM_TASK.
        """
        cfg = BitrixSettings.get_active()
        fields = {
            "TITLE": title[:250],
            "DESCRIPTION": description,
            "RESPONSIBLE_ID": responsible_id or cfg.default_assigned_by_id,
        }
        if deadline_iso:
            fields["DEADLINE"] = deadline_iso
        if deal_id:
            fields["UF_CRM_TASK"] = [f"D_{deal_id}"]

        try:
            resp = BitrixService.call("tasks.task.add", {"fields": fields})
            result = resp.get("result", {})
            task_id = str(result.get("task", {}).get("id") or result.get("id") or result)
            logger.info("Task created in Bitrix24 with ID %s for deal %s", task_id, deal_id)
            return task_id
        except Exception as e:
            logger.error("Failed to create task in Bitrix24: %s", e)
            return None

    @staticmethod
    def complete_task(task_id: str) -> bool:
        """Переводит задачу в статус 'Завершена' в Bitrix24 (tasks.task.complete)."""
        try:
            resp = BitrixService.call("tasks.task.complete", {"taskId": task_id})
            return bool(resp.get("result"))
        except Exception as e:
            logger.error("Failed to complete task %s in Bitrix24: %s", task_id, e)
            return False

    @staticmethod
    def delete_deal(deal_id: str) -> bool:
        """Удаляет сделку из Bitrix24 CRM (crm.deal.delete)."""
        try:
            resp = BitrixService.call("crm.deal.delete", {"id": deal_id})
            ok = bool(resp.get("result"))
            logger.info("Bitrix deal %s deleted: %s", deal_id, ok)
            return ok
        except Exception as e:
            logger.error("Failed to delete deal %s from Bitrix: %s", deal_id, e)
            return False

    @staticmethod
    def import_or_update_deal_from_bitrix(deal_data: Dict[str, Any]) -> Optional[Project]:
        """
        Импортирует сделку из Bitrix24 в локальную базу данных Mazory (Project):
        - Связывает/создает компанию и ответственного
        - Заполняет финансовые и описательные поля
        - Выставляет source='bitrix_crm'
        """
        bx_id = str(deal_data.get("ID"))
        if not bx_id:
            return None

        title = (deal_data.get("TITLE") or f"Сделка #{bx_id}").strip()
        amount = Decimal(str(deal_data.get("OPPORTUNITY") or 0.0))
        company_id = str(deal_data.get("COMPANY_ID") or "")
        assigned_id = str(deal_data.get("ASSIGNED_BY_ID") or "")
        stage_id = deal_data.get("STAGE_ID", "")
        status = BitrixService.stage_to_status(stage_id)

        # Менеджер
        manager = None
        if assigned_id:
            manager = UserProfile.objects.filter(bitrix_user_id=assigned_id).first()
            if not manager:
                manager = UserProfile.objects.filter(id=1).first()

        # Компания
        company = None
        if company_id:
            company = Company.objects.filter(bitrix_company_id=company_id).first()
            if not company:
                try:
                    c_resp = BitrixService.call("crm.company.get", {"id": company_id})
                    c_data = c_resp.get("result", {})
                    c_title = c_data.get("TITLE") or f"Компания #{company_id}"
                    company, _ = Company.objects.get_or_create(
                        bitrix_company_id=company_id,
                        defaults={"name": c_title, "client_type": "private"}
                    )
                except Exception:
                    pass

        contract_number = str(deal_data.get("UF_CRM_1731131779572") or "")[:250]
        deal_period = str(deal_data.get("UF_CRM_1778164670507") or "")[:120]
        direction = str(deal_data.get("UF_CRM_1778166248543") or "БТП")[:250]

        normalized = normalize_deal_name(title)
        cost_est = round(amount * Decimal('0.832'), 2)

        # Ищем проект по bitrix_id или по normalized_name
        project = Project.objects.filter(bitrix_id=bx_id).first()
        if not project and normalized:
            project = Project.objects.filter(normalized_name=normalized).first()

        if project:
            project.bitrix_id = bx_id
            project.name = title
            project.normalized_name = normalized
            project.contract_amount = amount
            if amount > 0 and project.cost_amount == Decimal('0.00'):
                project.cost_amount = cost_est
            project.status = status
            if company and not project.company:
                project.company = company
            if manager and not project.manager:
                project.manager = manager
            project.save()
            logger.info("Deal #%s '%s' updated in local DB", bx_id, title)
        else:
            project = Project.objects.create(
                bitrix_id=bx_id,
                name=title,
                normalized_name=normalized,
                source='bitrix_crm',
                company=company,
                manager=manager,
                equipment_type=direction or "БТП",
                contract_number=contract_number,
                deal_period=deal_period,
                status=status,
                contract_amount=amount,
                cost_amount=cost_est,
                paid_amount=amount if status == 'completed' else Decimal('0.00'),
                priority="A+++" if amount > 50000000 else "standard"
            )
            BusinessEvent.objects.create(
                event_type="deal_created_from_crm",
                project=project,
                manager=manager,
                title=f"Сделка импортирована из Bitrix24: {title}",
                description=f"Сумма: {amount:,.2f} ₸. ID в Bitrix24: #{bx_id}",
                severity="info"
            )
            logger.info("Deal #%s '%s' imported into local DB from Bitrix24", bx_id, title)

        return project

    @staticmethod
    def clean_duplicate_deals(dry_run: bool = False) -> Dict[str, Any]:
        """
        Устраняет дубликаты сделок в Bitrix24 CRM:
        - Группирует сделки по каноническому normalized_name
        - Оставляет самую раннюю Master-сделку
        - Удаляет дубликаты через crm.deal.delete
        - Добавляет аудит-комментарий в таймлайн мастер-сделки
        """
        logger.info("Starting Bitrix24 deal deduplication (dry_run=%s)...", dry_run)
        deals = BitrixService.fetch_all_paged("crm.deal.list", {
            "select": ["ID", "TITLE", "OPPORTUNITY", "DATE_CREATE", "STAGE_ID"]
        })

        groups: Dict[str, List[Dict[str, Any]]] = {}
        for d in deals:
            norm = normalize_deal_name(d.get("TITLE") or "")
            if norm:
                groups.setdefault(norm, []).append(d)

        deleted_ids: List[str] = []
        merged_groups: List[Dict[str, Any]] = []

        for norm_name, deal_list in groups.items():
            if len(deal_list) <= 1:
                continue

            # Сортируем по ID (самая ранняя сделка становится Master)
            deal_list.sort(key=lambda x: int(x.get("ID", 0)))
            master = deal_list[0]
            duplicates = deal_list[1:]

            master_id = str(master["ID"])
            dup_ids = [str(dup["ID"]) for dup in duplicates]

            logger.info("Found duplicates for '%s': Master #%s, Duplicates: %s", norm_name, master_id, dup_ids)

            if not dry_run:
                # Удаляем дубликаты в Bitrix24
                for dup_id in dup_ids:
                    try:
                        BitrixService.delete_deal(dup_id)
                        deleted_ids.append(dup_id)
                    except Exception as e:
                        logger.error("Failed to delete duplicate deal #%s: %s", dup_id, e)

                # Добавляем комментарий в таймлайн мастер-сделки
                comment = (
                    f"🤖 [Mazory Deduplication] Объединены дубликаты сделки:<br>"
                    f"Удалены дубли: {', '.join(['#' + did for did in dup_ids])}.<br>"
                    f"Сохранена мастер-сделка #{master_id}."
                )
                BitrixService.add_timeline_comment(master_id, comment)

                # Обновляем локальный Project
                Project.objects.filter(normalized_name=norm_name).update(bitrix_id=master_id)

            merged_groups.append({
                "name": norm_name,
                "master_id": master_id,
                "duplicates": dup_ids
            })

        return {
            "status": "success",
            "dry_run": dry_run,
            "total_deals_scanned": len(deals),
            "duplicate_groups_count": len(merged_groups),
            "deleted_count": len(deleted_ids) if not dry_run else sum(len(g["duplicates"]) for g in merged_groups),
            "merged_groups": merged_groups
        }

    @staticmethod
    def fetch_all_paged(method: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Универсальная выгрузка всех страниц из Bitrix24 REST API."""
        url_base = BitrixService.get_webhook_url()
        items = []
        start = 0
        p = params.copy() if params else {}

        while True:
            p['start'] = start
            res = requests.post(f"{url_base}{method}.json", json=p, timeout=25)
            data = res.json()
            result = data.get("result", [])
            if isinstance(result, dict) and "items" in result:
                result = result["items"]
            if not result:
                break
            items.extend(result)
            if "next" in data:
                start = data["next"]
            else:
                break
        return items

    @staticmethod
    def export_full_crm_seed() -> Dict[str, Any]:
        """Разовая полная выгрузка данных из Bitrix24."""
        logger.info("Starting Bitrix24 full CRM seed extraction...")
        users = BitrixService.fetch_all_paged("user.get", {"ADMIN_MODE": "true"})
        companies = BitrixService.fetch_all_paged("crm.company.list", {"select": ["ID", "TITLE", "COMPANY_TYPE", "PHONE", "EMAIL"]})
        deals = BitrixService.fetch_all_paged("crm.deal.list", {
            "select": ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "COMPANY_ID", "ASSIGNED_BY_ID",
                       "UF_CRM_1778164670507", "UF_CRM_1731131779572", "UF_CRM_1778166248543",
                       "DATE_CREATE", "MODIFY_BY_ID"]
        })
        contracts = BitrixService.fetch_all_paged("crm.item.list", {"entityTypeId": 1042})
        costs = BitrixService.fetch_all_paged("crm.item.list", {"entityTypeId": 1046})
        payments = BitrixService.fetch_all_paged("crm.item.list", {"entityTypeId": 1036})

        logger.info("Extraction finished: users=%d, companies=%d, deals=%d, contracts=%d, costs=%d, payments=%d",
                    len(users), len(companies), len(deals), len(contracts), len(costs), len(payments))

        return {
            "users": users,
            "companies": companies,
            "deals": deals,
            "contracts": contracts,
            "costs": costs,
            "payments": payments
        }
