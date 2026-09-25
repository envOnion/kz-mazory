import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from api.bitrix_service import BitrixService
from api.deduplication import normalize_deal_name
from api.models import (
    BitrixDealChangeLog,
    BusinessEvent,
    Company,
    Commitment,
    FinancialRecord,
    Project,
    RawMessage,
    UserProfile,
)

logger = logging.getLogger("mazory.actualization")


class Command(BaseCommand):
    help = "Полная сквозная актуализация сделок из архива WhatsApp чата и синхронизация с Bitrix24 CRM"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Режим симуляции: анализ без реальной записи в Bitrix24",
        )
        parser.add_argument(
            "--skip-catalog-sync",
            action="store_true",
            help="Пропустить предварительную выгрузку всех 576 сделок из Bitrix24",
        )
        parser.add_argument(
            "--skip-bitrix-push",
            action="store_true",
            help="Актуализировать локальную базу, но не отправлять изменения в Bitrix24",
        )
        parser.add_argument(
            "--archive-path",
            type=str,
            default="/app/archive_data",
            help="Путь к каталогу с messages.json и batches.json",
        )
        parser.add_argument(
            "--create-tasks-for-active",
            action="store_true",
            default=True,
            help="Создавать задачи в Bitrix24 только для актуальных дедлайнов (>= сегодня)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        skip_catalog_sync = options["skip_catalog_sync"]
        skip_bitrix_push = options["skip_bitrix_push"]
        archive_path = options["archive_path"]

        self.stdout.write(self.style.SUCCESS("=== ЗАПУСК ПОЛНОЙ АКТУАЛИЗАЦИИ CRM И ЧАТА ==="))
        self.stdout.write(f"Параметры: dry_run={dry_run}, skip_catalog={skip_catalog_sync}, skip_push={skip_bitrix_push}")

        # Шаг 1: Обеспечение профилей менеджеров
        self.stdout.write("\n[1/5] Проверка и создание профилей менеджеров...")
        managers = self._ensure_manager_profiles()
        self.stdout.write(f"Доступно менеджеров в системе: {len(managers)}")

        # Шаг 2: Синхронизация каталога всех сделок из Bitrix24
        if not skip_catalog_sync:
            self.stdout.write("\n[2/5] Синхронизация полного каталога сделок из Bitrix24 (все страницы)...")
            imported_count = self._sync_bitrix_deal_catalog()
            self.stdout.write(self.style.SUCCESS(f"Синхронизировано сделок из Bitrix24: {imported_count}"))
        else:
            self.stdout.write("\n[2/5] Пропуск синхронизации каталога Bitrix24 (--skip-catalog-sync)")

        # Шаг 3: Загрузка архива сообщений в RawMessage
        self.stdout.write(f"\n[3/5] Загрузка сырых сообщений из {archive_path}/messages.json...")
        raw_count = self._load_archive_messages(archive_path)
        self.stdout.write(self.style.SUCCESS(f"Загружено/проверено сообщений в RawMessage: {raw_count}"))

        # Шаг 4: Декомпозиция отчетов и извлечение коммерческих сущностей
        self.stdout.write(f"\n[4/5] Декомпозиция отчетов и извлечение сделок, оплат и обязательств...")
        deal_updates, commitments, financials = self._process_chat_history(archive_path, managers)
        self.stdout.write(self.style.SUCCESS(
            f"Извлечено фактов: {len(deal_updates)} сделок обновлено, "
            f"{len(commitments)} обязательств зафиксировано, "
            f"{len(financials)} финансовых записей создано"
        ))

        # Шаг 5: Синхронизация с Bitrix24 с защитой от спама и лимитом 2 req/s
        if not skip_bitrix_push:
            self.stdout.write("\n[5/5] Передача изменений в Bitrix24 CRM (троттлинг 0.7с)...")
            push_stats = self._push_updates_to_bitrix(dry_run=dry_run)
            self.stdout.write(self.style.SUCCESS(
                f"Результат передачи в Bitrix24:\n"
                f"  - Обновлено существующих сделок: {push_stats['updated_deals']}\n"
                f"  - Создано новых сделок в CRM: {push_stats['created_deals']}\n"
                f"  - Добавлено записей в таймлайн (хроника): {push_stats['timeline_comments']}\n"
                f"  - Создано актуальных задач: {push_stats['created_tasks']}\n"
                f"  - Зафиксировано логов в BitrixDealChangeLog: {push_stats['change_logs']}"
            ))
        else:
            self.stdout.write("\n[5/5] Передача в Bitrix24 пропущена (--skip-bitrix-push)")

        self.stdout.write(self.style.SUCCESS("\n=== АКТУАЛИЗАЦИЯ УСПЕШНО ЗАВЕРШЕНА ==="))

    def _ensure_manager_profiles(self) -> Dict[str, UserProfile]:
        """Создает профили сотрудников отдела продаж для привязки обязательств."""
        profiles_spec = [
            {"username": "kamil", "full_name": "Камиль", "role": "Ведущий менеджер по продажам", "phone": "375299231172"},
            {"username": "samat", "full_name": "Самат Ерланулы", "role": "Менеджер котельного оборудования", "phone": "77022223344"},
            {"username": "ulugbek", "full_name": "Улугбек", "role": "Менеджер тепловых пунктов и ЦТП", "phone": "77033334455"},
            {"username": "turar", "full_name": "Турар", "role": "Менеджер по тендерам и спецпроектам", "phone": "77011112233"},
            {"username": "vyacheslav", "full_name": "Вячеслав Медведев", "role": "Инженер / монтаж", "phone": "77055556677"},
            {"username": "zhanat", "full_name": "Жанат Бейсбаев", "role": "Ведущий менеджер по ключевым клиентам", "phone": "77011112244"},
            {"username": "director", "full_name": "Aqua kip engineering", "role": "Руководство / Директор", "phone": "77000000000"},
        ]
        managers = {}
        for p in profiles_spec:
            prof = UserProfile.objects.filter(full_name__icontains=p["full_name"]).first()
            if not prof:
                prof, _ = UserProfile.objects.get_or_create(
                    phone=p["phone"],
                    defaults={"full_name": p["full_name"], "role": p["role"]}
                )
            managers[p["full_name"].lower()] = prof
        return managers

    def _sync_bitrix_deal_catalog(self) -> int:
        """Выгружает все 576 сделок из Bitrix24 и сохраняет в локальный реестр Project."""
        deal_list = BitrixService.fetch_all_paged(
            "crm.deal.list",
            {
                "select": [
                    "ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "COMPANY_ID", "ASSIGNED_BY_ID",
                    "UF_CRM_1778164670507", "UF_CRM_1731131779572", "UF_CRM_1778166248543",
                    "DATE_CREATE"
                ]
            }
        )
        imported = 0
        for d in deal_list:
            try:
                BitrixService.import_or_update_deal_from_bitrix(d)
                imported += 1
            except Exception as e:
                logger.warning("Error importing deal %s: %s", d.get("ID"), e)
        return imported

    def _load_archive_messages(self, archive_path: str) -> int:
        """Загружает сообщения из archive_data/messages.json в RawMessage."""
        msg_file = os.path.join(archive_path, "messages.json")
        if not os.path.exists(msg_file):
            # Пробуем путь относительно проекта
            alt_path = os.path.join(os.path.dirname(__file__), "../../../archive_data/messages.json")
            if os.path.exists(alt_path):
                msg_file = alt_path
            else:
                self.stdout.write(self.style.WARNING(f"Файл {msg_file} не найден. Пропуск загрузки."))
                return RawMessage.objects.count()

        with open(msg_file, "r", encoding="utf-8") as f:
            messages_data = json.load(f)

        count = 0
        for item in messages_data:
            msg_id = item.get("whatsapp_message_id") or f"archive-{item.get('id')}"
            ts_str = item.get("message_timestamp")
            dt = timezone.now()
            if ts_str:
                try:
                    dt = timezone.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except Exception:
                    pass

            RawMessage.objects.get_or_create(
                message_id=msg_id,
                defaults={
                    "chat_id": "120363411153305864@g.us",
                    "sender_phone": item.get("sender_id") or "",
                    "sender_name": item.get("sender_name") or "",
                    "timestamp": dt,
                    "content": item.get("message_text") or "",
                    "raw_payload": item,
                    "processed": False
                }
            )
            count += 1
        return count

    def _find_manager(self, sender_name: str, managers: Dict[str, UserProfile]) -> Optional[UserProfile]:
        """Определяет профиль менеджера по имени отправителя или фрагменту."""
        s = sender_name.lower().strip()
        for k, v in managers.items():
            if k in s or s in k:
                return v
        return managers.get("камиль")

    def _match_project(self, object_name: str) -> Tuple[Optional[Project], bool]:
        """
        Сопоставляет название объекта с существующим Project в БД.
        Возвращает (project, is_exact_match).
        """
        clean_name = object_name.strip()
        norm = normalize_deal_name(clean_name)

        # 1. Точный поиск по bitrix_id (если передан #ID)
        id_match = re.search(r"#?(\d{3,5})\b", clean_name)
        if id_match:
            bx_id = id_match.group(1)
            p = Project.objects.filter(bitrix_id=bx_id).first()
            if p:
                return p, True

        # 2. Поиск по алиасам известных объектов
        aliases = {
            "оазис": "1396",
            "оазиз": "1396",
            "oasis": "1396",
            "343": "718",
            "цтп 343": "718",
            "top build": "718",
            "курмет": "350",
            "qurmet": "350",
            "олимпик": "304",
            "olympic": "304",
            "сенсата": "304",
            "медео": "1462",
            "medeo": "1462",
            "атылант": "868",
            "atylant": "868",
            "rayan": "1432",
            "раян": "1432",
            "пивзавод": "1058",
            "атамекен": "1446",
            "yer-khan": "1448",
            "ер-хан": "1448",
            "шокан": "1450",
            "уалиханов": "1450",
            "aspan": "1452",
            "аспан": "1452",
        }
        for alias, bx_id in aliases.items():
            if alias in clean_name.lower():
                p = Project.objects.filter(bitrix_id=bx_id).first()
                if p:
                    return p, True

        # 3. Поиск по нормализованному названию
        if norm:
            p = Project.objects.filter(normalized_name=norm).first()
            if p:
                return p, True

        # 4. Нечеткий поиск по вхождению названия
        p = Project.objects.filter(name__icontains=clean_name).first()
        if p:
            return p, True

        # Поиск по подстроке ключевых слов
        keywords = [w for w in re.split(r"[\s\-_«»\"]+", clean_name) if len(w) > 4]
        for kw in keywords:
            p = Project.objects.filter(name__icontains=kw).first()
            if p:
                return p, False

        return None, False

    def _decompose_multi_deal_reports(self, text: str, sender: str) -> List[Dict[str, Any]]:
        """
        Разбивает сложные ежедневные отчеты со списком объектов (1. ..., 2. ...)
        на структурированные фрагменты по конкретным объектам.
        """
        items = []
        # Разделители пунктов: "1. ", "2. ", "1) ", "**1. ", etc.
        pattern = r"(?:^|\n)(?:\*{0,2}(?:\d+[\.\)]|[A-ZА-ЯЁ][\.\)]|\-)\s*\*{0,2})([^\n]+)(.*?)(?=(?:\n\*{0,2}(?:\d+[\.\)]|[A-ZА-ЯЁ][\.\)]|\-)\s*\*{0,2}[^\n]+|$))"
        matches = list(re.finditer(pattern, text, re.DOTALL))

        if matches and len(matches) >= 2:
            for m in matches:
                title_line = m.group(1).strip().strip("*").strip()
                body = m.group(2).strip()
                full_block = f"{title_line}\n{body}".strip()

                # Извлекаем название объекта (до тире или слэша)
                obj_name = re.split(r"\s+[—–\-/]\s+", title_line)[0].strip()
                obj_name = re.sub(r"^(Проект|Объект|ЖК|БЦ|ТОО)\s+", "", obj_name, flags=re.IGNORECASE)

                if len(obj_name) >= 3 and not any(skip in obj_name.lower() for skip in ["коммерческие предложения", "договоры с контрагентами", "итог", "план"]):
                    items.append({
                        "object_name": obj_name,
                        "raw_title": title_line,
                        "content": full_block,
                        "sender": sender
                    })

        return items

    def _extract_facts_from_text(self, text: str, default_obj_name: str = "") -> Dict[str, Any]:
        """
        Извлекает финансовые суммы, этапы, обязательства и блокеры из текста.
        """
        facts: Dict[str, Any] = {
            "object_name": default_obj_name,
            "contract_amount": None,
            "paid_amount": None,
            "cost_amount": None,
            "stage": None,
            "current_action": text[:350].strip(),
            "next_action": "",
            "deadline": None,
            "blocker": ""
        }

        # 1. Поиск сумм (например: "58,5 млн тенге", "42 млн ₸", "92 500 000 тг", "41 млн")
        million_match = re.search(r"(\d+[\.,]?\d*)\s*(?:млн|миллион[а-ов]?)\s*(?:тенге|тг|₸)?", text, re.IGNORECASE)
        if million_match:
            try:
                num = float(million_match.group(1).replace(",", "."))
                facts["contract_amount"] = Decimal(str(int(num * 1_000_000)))
            except Exception:
                pass

        exact_amount_match = re.search(r"(\d{1,3}(?:\s+\d{3}){1,3})\s*(?:тенге|тг|₸)", text, re.IGNORECASE)
        if exact_amount_match and not facts["contract_amount"]:
            try:
                clean_num = exact_amount_match.group(1).replace(" ", "")
                facts["contract_amount"] = Decimal(clean_num)
            except Exception:
                pass

        # 2. Оплаты (получили оплату, поступили деньги, оплачено 42 млн)
        payment_match = re.search(r"(?:оплат[аеы]|поступил[ао]|выставил[и]? счет на)\s+(\d+[\.,]?\d*)\s*(?:млн|миллион[а-ов]?)", text, re.IGNORECASE)
        if payment_match:
            try:
                num = float(payment_match.group(1).replace(",", "."))
                facts["paid_amount"] = Decimal(str(int(num * 1_000_000)))
            except Exception:
                pass

        # 3. Себестоимость
        cost_match = re.search(r"(?:себестоимость|себес)\s+(?:составил[а]?\s+)?(\d+[\.,]?\d*)\s*(?:млн|миллион[а-ов]?)", text, re.IGNORECASE)
        if cost_match:
            try:
                num = float(cost_match.group(1).replace(",", "."))
                facts["cost_amount"] = Decimal(str(int(num * 1_000_000)))
            except Exception:
                pass

        # 4. Стадия (stage)
        text_lower = text.lower()
        if "договор подписан" in text_lower or "подписали договор" in text_lower:
            facts["stage"] = "contract_signed"
        elif "на подписание" in text_lower or "подписание договора" in text_lower or "согласование договора" in text_lower:
            facts["stage"] = "contract_signing"
        elif "тендер" in text_lower:
            facts["stage"] = "tender"
        elif "кп отправлен" in text_lower or "выдать кп" in text_lower or "просчет кп" in text_lower:
            facts["stage"] = "proposal"
        elif "ожидание оплаты" in text_lower or "выставлен счет" in text_lower:
            facts["stage"] = "payment_pending"
        elif "монтаж" in text_lower or "исполнени" in text_lower or "заказ оборудования" in text_lower:
            facts["stage"] = "executing"

        # 5. Следующий шаг и дедлайн
        next_step_match = re.search(r"(?:завтра|планируется|на завтра|необходимо|нужно)\s+([^\.\n]+[\.\n]?)", text, re.IGNORECASE)
        if next_step_match:
            facts["next_action"] = next_step_match.group(0).strip()
            # Дата дедлайна
            if "завтра" in text_lower:
                facts["deadline"] = timezone.now().date() + timedelta(days=1)
            elif "в субботу" in text_lower:
                facts["deadline"] = timezone.now().date() + timedelta(days=2)
            else:
                facts["deadline"] = timezone.now().date() + timedelta(days=3)

        # 6. Блокеры
        if "не согласован" in text_lower or "блокер" in text_lower or "задержк" in text_lower or "перенеслась" in text_lower:
            facts["blocker"] = "Зафиксирован блокер / перенос сроков в чате"

        return facts

    def _process_chat_history(
        self, archive_path: str, managers: Dict[str, UserProfile]
    ) -> Tuple[List[Project], List[Commitment], List[FinancialRecord]]:
        """
        Основной цикл обработки истории чата:
        1. Читает batches.json (пре-анализ)
        2. Декомпозирует многообъектные отчеты
        3. Обновляет Project, создает Commitment и FinancialRecord
        """
        batches_file = os.path.join(archive_path, "batches.json")
        batches = []
        if os.path.exists(batches_file):
            with open(batches_file, "r", encoding="utf-8") as f:
                batches = json.load(f)

        updated_projects: List[Project] = []
        created_commitments: List[Commitment] = []
        created_financials: List[FinancialRecord] = []

        # Каталог разобранных фактов для применения
        extracted_items = []

        # А. Обработка существующих пачек
        for b in batches:
            b_type = b.get("ai_type")
            text = b.get("combined_text") or ""
            sender = b.get("sender_name") or "Коллега"
            ai_res = b.get("ai_result") or {}

            # Проверяем, есть ли разбивка по нескольким объектам
            decomposed = self._decompose_multi_deal_reports(text, sender)
            if decomposed:
                for dec in decomposed:
                    facts = self._extract_facts_from_text(dec["content"], dec["object_name"])
                    extracted_items.append((dec["object_name"], facts, dec["content"], sender))
            elif b_type in ["deal_update", "new_deal"]:
                # Одиночная сделка из пакета
                obj_name = ai_res.get("object_name") or ""
                if not obj_name:
                    first_line = text.split("\n")[0]
                    obj_name = re.sub(r"^(По|Отчет|ЖК|БЦ)\s+", "", first_line).split(",")[0].split(".")[0].strip()

                facts = {
                    "object_name": obj_name,
                    "contract_amount": Decimal(str(ai_res.get("amount"))) if ai_res.get("amount") else None,
                    "cost_amount": Decimal(str(ai_res.get("cost"))) if ai_res.get("cost") else None,
                    "paid_amount": Decimal(str(ai_res.get("amount"))) if "оплат" in text.lower() and ai_res.get("amount") else None,
                    "stage": ai_res.get("stage"),
                    "current_action": (ai_res.get("reason") or text[:250]).strip(),
                    "next_action": text[:150],
                    "deadline": timezone.now().date() + timedelta(days=2),
                    "blocker": ai_res.get("blocker") or ""
                }
                extracted_items.append((obj_name, facts, text, sender))

        self.stdout.write(f"Сформировано рабочих сущностей для сопоставления: {len(extracted_items)}")

        # Б. Сопоставление и сохранение в базу данных
        for obj_name, facts, raw_text, sender in extracted_items:
            if not obj_name or len(obj_name) < 3:
                continue

            manager = self._find_manager(sender, managers)
            project, is_exact = self._match_project(obj_name)

            if not project:
                # Новая сделка, если есть сумма или явный интерес
                norm = normalize_deal_name(obj_name) or obj_name
                amount = facts.get("contract_amount") or Decimal("0.00")
                cost = facts.get("cost_amount") or (amount * Decimal("0.832"))

                project = Project.objects.create(
                    name=obj_name,
                    normalized_name=norm,
                    source="chat",
                    manager=manager,
                    status=facts.get("stage") or "qualification",
                    contract_amount=amount,
                    cost_amount=cost,
                    paid_amount=facts.get("paid_amount") or Decimal("0.00"),
                    current_action=facts.get("current_action") or "",
                    next_action=facts.get("next_action") or "",
                    blocker=facts.get("blocker") or "",
                    needs_bitrix_sync=True,
                    last_chat_activity_at=timezone.now()
                )
                BusinessEvent.objects.create(
                    event_type="new_deal",
                    project=project,
                    manager=manager,
                    title=f"Новая сделка из чата: {project.name}",
                    description=f"Сумма: {project.contract_amount:,.2f} ₸. Ответственный: {manager.full_name if manager else sender}",
                    severity="info"
                )
                updated_projects.append(project)
            else:
                # Обновление существующей сделки
                updated = False
                if facts.get("contract_amount") and facts["contract_amount"] > 0:
                    project.contract_amount = facts["contract_amount"]
                    updated = True
                if facts.get("cost_amount") and facts["cost_amount"] > 0:
                    project.cost_amount = facts["cost_amount"]
                    updated = True
                if facts.get("paid_amount") and facts["paid_amount"] > 0:
                    project.paid_amount += facts["paid_amount"]
                    updated = True
                if facts.get("stage"):
                    project.status = facts["stage"]
                    updated = True
                if facts.get("current_action"):
                    project.current_action = facts["current_action"]
                    updated = True
                if facts.get("next_action"):
                    project.next_action = facts["next_action"]
                    updated = True
                if facts.get("blocker"):
                    project.blocker = facts["blocker"]
                    updated = True

                project.needs_bitrix_sync = True
                project.last_chat_activity_at = timezone.now()
                if manager and not project.manager:
                    project.manager = manager
                project.save()
                updated_projects.append(project)

            # В. Создание обязательств (Commitment)
            next_action = facts.get("next_action")
            deadline = facts.get("deadline") or (timezone.now().date() + timedelta(days=2))
            if next_action and len(next_action) > 10:
                is_active = deadline >= timezone.now().date()
                comm, created = Commitment.objects.get_or_create(
                    project=project,
                    commitment_text=next_action[:500],
                    defaults={
                        "manager": manager,
                        "deadline": deadline,
                        "status": "pending" if is_active else "completed",
                        "severity": "critical" if ("срочно" in raw_text.lower() or "договор" in next_action.lower()) else "medium"
                    }
                )
                if created:
                    created_commitments.append(comm)

            # Г. Создание финансовых записей (FinancialRecord)
            paid_amt = facts.get("paid_amount")
            if paid_amt and paid_amt > 0:
                fin, created = FinancialRecord.objects.get_or_create(
                    project=project,
                    amount=paid_amt,
                    payment_date=timezone.now().date(),
                    defaults={
                        "payment_type": "milestone",
                        "status": "received",
                        "notes": f"Зафиксировано из отчета {sender}: {raw_text[:120]}"
                    }
                )
                if created:
                    created_financials.append(fin)

        return updated_projects, created_commitments, created_financials

    def _push_updates_to_bitrix(self, dry_run: bool = False) -> Dict[str, int]:
        """
        Передает обновленные сделки в Bitrix24 с троттлингом (0.7с)
        и логирует каждое действие в BitrixDealChangeLog.
        """
        stats = {
            "updated_deals": 0,
            "created_deals": 0,
            "timeline_comments": 0,
            "created_tasks": 0,
            "change_logs": 0
        }

        projects_to_sync = Project.objects.filter(needs_bitrix_sync=True)
        total = projects_to_sync.count()
        self.stdout.write(f"Сделок, готовых к синхронизации с Bitrix24: {total}")

        today = timezone.now().date()

        for idx, p in enumerate(projects_to_sync, start=1):
            self.stdout.write(f"[{idx}/{total}] Синхронизация сделки '{p.name}' (ID Bitrix: {p.bitrix_id or 'Новая'})...")

            if dry_run:
                stats["updated_deals"] += 1
                continue

            # 1. Обновление или создание в Bitrix
            if p.bitrix_id:
                # Существующая сделка
                fields = {
                    "STAGE_ID": BitrixService.status_to_stage(p.status),
                    "COMMENTS": f"Актуализировано из WhatsApp Mazory: {p.current_action[:300]}"
                }
                if p.contract_amount > 0:
                    fields["OPPORTUNITY"] = str(p.contract_amount)

                # Вызов update_deal (внутри автоматически вызывается _record_deal_log)
                success = BitrixService.update_deal(
                    deal_id=p.bitrix_id,
                    fields=fields,
                    project=p,
                    triggered_by="chat_actualization"
                )
                if success:
                    stats["updated_deals"] += 1
                time.sleep(0.7)  # Троттлинг 2 req/s
            else:
                # Новая сделка
                new_deal_data = {
                    "TITLE": p.name,
                    "STAGE_ID": BitrixService.status_to_stage(p.status),
                    "OPPORTUNITY": str(p.contract_amount) if p.contract_amount > 0 else "0.00",
                    "COMMENTS": f"Создано автоматически из чата WhatsApp Mazory. Текущий статус: {p.current_action}"
                }
                new_bx_id = BitrixService.create_deal(
                    project_data=new_deal_data,
                    project=p,
                    triggered_by="chat_actualization"
                )
                if new_bx_id:
                    p.bitrix_id = new_bx_id
                    p.save(update_fields=["bitrix_id"])
                    stats["created_deals"] += 1
                time.sleep(0.7)

            # 2. Добавление структурированного комментария в таймлайн сделки
            if p.bitrix_id:
                comment_html = (
                    f"<b>🤖 [Mazory AI Актуализация из WhatsApp]</b><br>"
                    f"<b>Текущее состояние:</b> {p.current_action}<br>"
                    f"<b>Следующий шаг:</b> {p.next_action or 'Не указан'}<br>"
                )
                if p.paid_amount > 0:
                    comment_html += f"<b>Оплачено:</b> {p.paid_amount:,.2f} ₸<br>"
                if p.blocker:
                    comment_html += f"<b>⚠️ Блокер:</b> {p.blocker}<br>"

                BitrixService.add_timeline_comment(p.bitrix_id, comment_html)
                stats["timeline_comments"] += 1
                time.sleep(0.7)

            # 3. Создание задач в Bitrix24 только для актуальных обязательств
            active_commitments = Commitment.objects.filter(
                project=p,
                bitrix_task_id__isnull=True,
                deadline__gte=today
            )
            for comm in active_commitments:
                task_title = f"[Mazory] {comm.commitment_text[:80]}"
                task_desc = f"Сделка: {p.name}\nОбязательство: {comm.commitment_text}\nСрок: {comm.deadline}"
                task_id = BitrixService.create_task(
                    title=task_title,
                    description=task_desc,
                    deadline_iso=comm.deadline.isoformat(),
                    deal_id=p.bitrix_id
                )
                if task_id:
                    comm.bitrix_task_id = task_id
                    comm.save(update_fields=["bitrix_task_id"])
                    stats["created_tasks"] += 1
                time.sleep(0.7)

            # Снимаем флаг needs_bitrix_sync
            p.needs_bitrix_sync = False
            p.save(update_fields=["needs_bitrix_sync"])

        stats["change_logs"] = BitrixDealChangeLog.objects.filter(triggered_by="chat_actualization").count()
        return stats
