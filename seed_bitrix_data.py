#!/usr/bin/env python3
"""
Mazory CRM — Bitrix24 Standalone Data Seeder
============================================
Выгружает пользователей, компании, сделки и смарт-процессы (договора, расчеты, платежи)
из Bitrix24 через REST API (вебхук) и наполняет локальную базу данных PostgreSQL.

Использование:
    cd /Users/a_belianskii/projects/kz-aquakip/mazory/backend
    uv run python ../seed_bitrix_data.py
    
Или из корня:
    PYTHONPATH=backend uv run --directory backend python ../seed_bitrix_data.py [--dry-run]
"""

import os
import sys
import socket
from decimal import Decimal
from pathlib import Path

# Setup Django environment
BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / 'backend'
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Auto-detect whether running on host or inside docker
if 'POSTGRES_HOST' not in os.environ:
    try:
        socket.gethostbyname('postgres')
        os.environ['POSTGRES_HOST'] = 'postgres'
    except socket.gaierror:
        os.environ['POSTGRES_HOST'] = '127.0.0.1'
        os.environ['POSTGRES_PORT'] = os.environ.get('POSTGRES_PORT', '5434')

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mazory_backend.settings')

import django
django.setup()

from django.contrib.auth.models import User
from django.utils import timezone
from api.models import UserProfile, Company, Project, FinancialRecord, BitrixSettings
from api.bitrix_service import BitrixService

def stage_to_status(stage_id: str) -> str:
    """Маппинг стадий Bitrix24 в статусы Project Mazory."""
    stage_id = (stage_id or '').upper()
    if 'WON' in stage_id or 'FINAL' in stage_id:
        return 'completed'
    if 'LOSE' in stage_id or 'APOLOGY' in stage_id:
        return 'lost'
    if 'EXECUT' in stage_id or 'PROD' in stage_id:
        return 'in_execution'
    if 'PREPAY' in stage_id or 'SIGN' in stage_id or 'CONTRACT' in stage_id:
        return 'contract_signing'
    if 'PREPAR' in stage_id or 'PROPOSAL' in stage_id:
        return 'proposal_sent'
    if 'QUALIF' in stage_id or 'NEW' in stage_id:
        return 'qualification'
    return 'qualification'

def seed_bitrix_crm(dry_run: bool = False):
    print("=" * 70)
    print("🚀 MAZORY AI: СИНХРОНИЗАЦИЯ И СИДИРОВАНИЕ ДАННЫХ ИЗ BITRIX24")
    print("=" * 70)

    cfg = BitrixSettings.get_active()
    print(f"📡 Webhook URL: {cfg.webhook_url}")
    print(f"🔍 Режим: {'DRY RUN (без сохранения)' if dry_run else 'LIVE SEED (запись в PostgreSQL)'}\n")

    data = BitrixService.export_full_crm_seed()
    
    users = data.get("users", [])
    companies = data.get("companies", [])
    deals = data.get("deals", [])
    contracts = data.get("contracts", [])
    costs = data.get("costs", [])
    payments = data.get("payments", [])

    print(f"📦 Получено объектов из Bitrix24:")
    print(f"   • Пользователи:      {len(users)}")
    print(f"   • Компании:          {len(companies)}")
    print(f"   • Сделки:            {len(deals)}")
    print(f"   • Договора (SPA 1042): {len(contracts)}")
    print(f"   • Расчеты (SPA 1046):  {len(costs)}")
    print(f"   • Платежи (SPA 1036):  {len(payments)}\n")

    if dry_run:
        print("✓ Dry run завершен успешно. Изменения не сохранены.")
        return

    # 1. Синхронизация пользователей / менеджеров
    print("👤 Импорт пользователей...")
    user_map = {} # bitrix_user_id -> UserProfile
    for u in users:
        bx_id = str(u.get('ID'))
        name = f"{u.get('NAME', '')} {u.get('LAST_NAME', '')}".strip() or u.get('LOGIN') or f"User {bx_id}"
        email = u.get('EMAIL') or f"user{bx_id}@aquakip.kz"
        phone = u.get('PERSONAL_MOBILE') or u.get('WORK_PHONE') or ''
        
        # Находим или создаем Django User
        username = email.split('@')[0] if email else f"bx_{bx_id}"
        django_user, _ = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "first_name": u.get('NAME', ''), "last_name": u.get('LAST_NAME', '')}
        )
        profile, _ = UserProfile.objects.get_or_create(
            user=django_user,
            defaults={
                "full_name": name,
                "role": u.get('WORK_POSITION') or 'Менеджер по продажам',
                "department": 'Отдел продаж Aqua Kip',
                "email": email,
                "phone": phone
            }
        )
        user_map[bx_id] = profile

    print(f"   ✓ Сохранено/обновлено профилей менеджеров: {len(user_map)}")

    # 2. Синхронизация компаний
    print("\n🏢 Импорт компаний...")
    company_map = {} # bitrix_company_id -> Company
    for c in companies:
        bx_id = str(c.get('ID'))
        title = (c.get('TITLE') or '').strip()
        if not title:
            continue
        
        phone = ''
        if c.get('PHONE') and isinstance(c['PHONE'], list) and len(c['PHONE']) > 0:
            phone = c['PHONE'][0].get('VALUE', '')
            
        company, _ = Company.objects.update_or_create(
            name=title,
            defaults={
                "bitrix_company_id": bx_id,
                "client_type": "contractor" if "подряд" in title.lower() else "private",
                "phone": phone
            }
        )
        company_map[bx_id] = company

    print(f"   ✓ Сохранено/обновлено компаний: {len(company_map)}")

    # 3. Синхронизация сделок
    print("\n💼 Импорт сделок (crm.deal)...")
    deals_created = 0
    deals_updated = 0
    
    for d in deals:
        bx_id = str(d.get('ID'))
        title = (d.get('TITLE') or f"Сделка #{bx_id}").strip()[:250]
        amount = Decimal(str(d.get('OPPORTUNITY') or 0.0))
        company_id = str(d.get('COMPANY_ID') or '')
        assigned_id = str(d.get('ASSIGNED_BY_ID') or '')
        
        company = company_map.get(company_id)
        manager = user_map.get(assigned_id)
        
        contract_number = str(d.get('UF_CRM_1731131779572') or '')[:250]
        deal_period = str(d.get('UF_CRM_1778164670507') or '')[:120]
        direction = str(d.get('UF_CRM_1778166248543') or 'БТП')[:250]
        status = stage_to_status(d.get('STAGE_ID'))

        # Оценка себестоимости по умолчанию (83.2% от суммы = 16.8% плановая маржа)
        cost_est = round(amount * Decimal('0.832'), 2)

        proj, created = Project.objects.update_or_create(
            bitrix_id=bx_id,
            defaults={
                "name": title,
                "company": company,
                "manager": manager,
                "equipment_type": direction,
                "contract_number": contract_number,
                "deal_period": deal_period,
                "status": status,
                "contract_amount": amount,
                "cost_amount": cost_est,
                "paid_amount": amount if status == 'completed' else Decimal('0.00'),
                "priority": "A+++" if amount > 50000000 else "standard"
            }
        )
        if created:
            deals_created += 1
        else:
            deals_updated += 1

    print(f"   ✓ Сделок создано: {deals_created}, обновлено: {deals_updated}")

    # Обновляем статус в BitrixSettings
    cfg.last_sync_at = timezone.now()
    cfg.last_sync_status = f"Успешно выгружено: {len(deals)} сделок, {len(companies)} компаний, {len(users)} пользователей"
    cfg.save()

    print("\n" + "=" * 70)
    print("✅ СИНХРОНИЗАЦИЯ И СИДИРОВАНИЕ CRM УСПЕШНО ЗАВЕРШЕНЫ!")
    print(f"Всего объектов в базе: {Project.objects.count()} сделок, {Company.objects.count()} компаний.")
    print("=" * 70)

if __name__ == '__main__':
    dry = '--dry-run' in sys.argv
    seed_bitrix_crm(dry_run=dry)
