import logging
from typing import Dict, Any, Optional, List
import requests
from api.models import BitrixSettings

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
    def create_deal(project_data: Dict[str, Any]) -> Optional[str]:
        """
        Создает сделку в Bitrix24 CRM через метод crm.deal.add.
        Заполняет поля: Название, Сумма, Ответственный, Объекты, Оборудования, Период.
        """
        cfg = BitrixSettings.get_active()
        if not cfg.is_active or not cfg.auto_create_deals:
            logger.info("Bitrix deal auto-creation is disabled in BitrixSettings")
            return None

        title = project_data.get("name") or project_data.get("object_name") or "Новая сделка из WhatsApp"
        amount = project_data.get("contract_amount") or project_data.get("amount") or 0.0

        fields = {
            "TITLE": title,
            "OPPORTUNITY": float(amount),
            "CURRENCY_ID": "KZT",
            "STAGE_ID": "NEW",
            "CATEGORY_ID": cfg.deal_category_id,
            "ASSIGNED_BY_ID": project_data.get("assigned_by_id") or cfg.default_assigned_by_id,
            # Кастомные поля AquaKip
            "UF_CRM_1731131779572": title, # Объекты
            "UF_CRM_1778166248543": project_data.get("direction") or project_data.get("equipment_type") or "", # Оборудования
            "UF_CRM_1778164670507": project_data.get("deal_period") or "", # Период сделки
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
    def delete_deal(deal_id: str) -> bool:
        """
        Удаляет сделку из Bitrix24 CRM (crm.deal.delete).
        Используется в тестах и для очистки тестовых данных.
        """
        try:
            resp = BitrixService.call("crm.deal.delete", {"id": deal_id})
            ok = bool(resp.get("result"))
            logger.info("Bitrix deal %s deleted: %s", deal_id, ok)
            return ok
        except Exception as e:
            logger.error("Failed to delete deal %s from Bitrix: %s", deal_id, e)
            return False

    @staticmethod
    def fetch_all_paged(method: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Универсальная выгрузка всех страниц из Bitrix24 REST API.
        """
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
        """
        Разовая полная выгрузка данных из Bitrix24:
        - Пользователи
        - Компании
        - Сделки
        - Договора (SPA 1042)
        - Расчеты/Себестоимость (SPA 1046)
        - Платежи (SPA 1036)
        """
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
