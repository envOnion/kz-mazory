from decimal import Decimal
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta, date
from api.models import UserProfile, Company, Project, Commitment, FinancialRecord, RawMessage, BusinessEvent
from api.qdrant_service import qdrant_service

class Command(BaseCommand):
    help = "Заполняет базу данных реальными данными компании Aqua Kip из истории переписки"

    def handle(self, *args, **options):
        self.stdout.write("Сеем реальные данные Aqua Kip Engineering...")

        # 1. Создаем менеджеров
        managers_data = [
            {
                "username": "zhanat",
                "name": "Жанат Бейсбаев",
                "role": "Ведущий менеджер по ключевым клиентам",
                "phone": "+7 (701) 111-22-33",
                "target": Decimal('570000000.00'),
                "sales": Decimal('568270000.00'),
                "deals": 40,
                "rank": 1,
                "conversion": Decimal('95.0'),
                "avatar": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?auto=format&fit=crop&w=200&q=80"
            },
            {
                "username": "samat",
                "name": "Самат Ерланулы",
                "role": "Менеджер проектов и котельного оборудования",
                "phone": "+7 (702) 222-33-44",
                "target": Decimal('280000000.00'),
                "sales": Decimal('278160000.00'),
                "deals": 54,
                "rank": 2,
                "conversion": Decimal('42.0'),
                "avatar": "https://images.unsplash.com/photo-1500648767791-00dcc994a43e?auto=format&fit=crop&w=200&q=80"
            },
            {
                "username": "ulugbek",
                "name": "Улугбек",
                "role": "Менеджер направления тепловых пунктов и ЦТП",
                "phone": "+7 (703) 333-44-55",
                "target": Decimal('140000000.00'),
                "sales": Decimal('138130000.00'),
                "deals": 22,
                "rank": 3,
                "conversion": Decimal('38.5'),
                "avatar": "https://images.unsplash.com/photo-1472099645785-5658abf4ff4e?auto=format&fit=crop&w=200&q=80"
            },
            {
                "username": "kamil",
                "name": "Камиль",
                "role": "Старший менеджер по корпоративным застройщикам",
                "phone": "+7 (701) 987-65-43",
                "target": Decimal('45000000.00'),
                "sales": Decimal('31790000.00'),
                "deals": 37,
                "rank": 4,
                "conversion": Decimal('68.0'),
                "avatar": "https://images.unsplash.com/photo-1519085360753-af0119f7cbe7?auto=format&fit=crop&w=200&q=80"
            },
            {
                "username": "vyacheslav",
                "name": "Вячеслав Медведев",
                "role": "Генеральный директор / Руководитель продаж",
                "phone": "+7 (777) 555-66-77",
                "target": Decimal('35000000.00'),
                "sales": Decimal('33190000.00'),
                "deals": 10,
                "rank": 5,
                "conversion": Decimal('80.0'),
                "avatar": "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=200&q=80"
            },
            {
                "username": "turar",
                "name": "Турар",
                "role": "Менеджер регионального развития и тендеров",
                "phone": "+7 (705) 777-88-99",
                "target": Decimal('25000000.00'),
                "sales": Decimal('12500000.00'),
                "deals": 11,
                "rank": 6,
                "conversion": Decimal('25.0'),
                "avatar": "https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?auto=format&fit=crop&w=200&q=80"
            }
        ]

        managers = {}
        for md in managers_data:
            user, _ = User.objects.get_or_create(username=md["username"], defaults={"email": f"{md['username']}@aquakip.kz"})
            user.set_password("mazory2026")
            user.save()

            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.full_name = md["name"]
            profile.role = md["role"]
            profile.phone = md["phone"]
            profile.monthly_target = md["target"]
            profile.current_sales = md["sales"]
            profile.deals_count = md["deals"]
            profile.rank_in_team = md["rank"]
            profile.conversion_rate = md["conversion"]
            profile.avatar_url = md["avatar"]
            profile.save()
            managers[md["username"]] = profile

        # 2. Создаем Компании
        companies_data = [
            ("Qazaq Stroy", "private", "Болат", "+7 701 100-01-01"),
            ("Top Build", "contractor", "Снабжение г. Семей", "+7 722 200-02-02"),
            ("Monolit Group", "private", "Салтанат", "+7 701 300-03-03"),
            ("Sensata Group", "private", "Арман (нач. закупа)", "+7 701 400-04-04"),
            ("Integra Construction", "contractor", "Ердос / Мадина", "+7 701 500-05-05"),
            ("Tansu Construction", "contractor", "Николай Юрьевич", "+7 701 600-06-06"),
            ("BI Group", "private", "Ардак (отдел закупа)", "+7 701 700-07-07"),
            ("Sardar Group", "private", "Бекжан", "+7 701 800-08-08"),
            ("ГКП Алматы Су", "quasi_state", "Мурат", "+7 727 300-09-09"),
            ("Vertex AG", "private", "Дирекция закупа", "+7 701 900-10-10"),
            ("Everest Development", "private", "Руководитель проекта", "+7 701 999-11-11"),
        ]
        companies = {}
        for name, ctype, contact, phone in companies_data:
            c, _ = Company.objects.get_or_create(
                name=name,
                defaults={"client_type": ctype, "contact_person": contact, "phone": phone}
            )
            companies[name] = c

        # 3. Создаем Проекты
        projects_data = [
            {
                "name": "ПСЭМ-01-2026 (190 Гкал)",
                "company": "Qazaq Stroy",
                "manager": "zhanat",
                "type": "private",
                "status": "in_execution",
                "contract": Decimal('1425000000.00'),
                "cost": Decimal('1225000000.00'),
                "margin": Decimal('14.00'),
                "paid": Decimal('285000000.00'),
                "due": Decimal('1140000000.00'),
                "equipment": "БТП + Модульные узлы",
                "priority": "A+++"
            },
            {
                "name": "ПСЭМ-02-2026 (100 Гкал)",
                "company": "Qazaq Stroy",
                "manager": "zhanat",
                "type": "private",
                "status": "in_execution",
                "contract": Decimal('899900000.00'),
                "cost": Decimal('770000000.00'),
                "margin": Decimal('14.40'),
                "paid": Decimal('109980000.00'),
                "due": Decimal('789920000.00'),
                "equipment": "БТП 100 Гкал",
                "priority": "A+++"
            },
            {
                "name": "Вилла — котельная 16,8 МВт",
                "company": "Qazaq Stroy",
                "manager": "zhanat",
                "type": "private",
                "status": "in_execution",
                "contract": Decimal('522000000.00'),
                "cost": Decimal('442500000.00'),
                "margin": Decimal('15.20'),
                "paid": Decimal('79500000.00'),
                "due": Decimal('442500000.00'),
                "equipment": "Котельная 16.8 МВт",
                "priority": "A++"
            },
            {
                "name": "Алтын Сити, 3 очередь (37–48)",
                "company": "Qazaq Stroy",
                "manager": "zhanat",
                "type": "private",
                "status": "completed",
                "contract": Decimal('110900000.00'),
                "cost": Decimal('59530000.00'),
                "margin": Decimal('46.30'),
                "paid": Decimal('110900000.00'),
                "due": Decimal('0.00'),
                "equipment": "БТП + Насосы",
                "priority": "A+"
            },
            {
                "name": "ЦТП 343 квартал, г. Семей",
                "company": "Top Build",
                "manager": "ulugbek",
                "type": "contractor",
                "status": "in_execution",
                "contract": Decimal('204170000.00'),
                "cost": Decimal('173540000.00'),
                "margin": Decimal('15.00'),
                "paid": Decimal('142080000.00'),
                "due": Decimal('62090000.00'),
                "equipment": "ЦТП / Насосы / Danfoss",
                "priority": "A++"
            },
            {
                "name": "ЖК Vertex Garden — котельная 10 МВт",
                "company": "Vertex AG",
                "manager": "kamil",
                "type": "private",
                "status": "contract_signing",
                "contract": Decimal('156000000.00'),
                "cost": Decimal('127920000.00'),
                "margin": Decimal('18.00'),
                "paid": Decimal('46800000.00'),
                "due": Decimal('109200000.00'),
                "equipment": "БМК 10 МВт",
                "priority": "A++"
            },
            {
                "name": "ЖК Курмет — временная котельная 1 МВт",
                "company": "Monolit Group",
                "manager": "kamil",
                "type": "private",
                "status": "proposal_sent",
                "contract": Decimal('38000000.00'),
                "cost": Decimal('28000000.00'),
                "margin": Decimal('26.30'),
                "paid": Decimal('0.00'),
                "due": Decimal('38000000.00'),
                "equipment": "БМК 1 МВт (бартер 60/40)",
                "priority": "A+"
            },
            {
                "name": "КЭС Курчатов — угольная БМК 8 МВт",
                "company": "Tansu Construction",
                "manager": "samat",
                "type": "contractor",
                "status": "design",
                "contract": Decimal('479000000.00'),
                "cost": Decimal('407150000.00'),
                "margin": Decimal('15.00'),
                "paid": Decimal('0.00'),
                "due": Decimal('479000000.00'),
                "equipment": "Угольная котельная 8 МВт",
                "priority": "A+++"
            },
            {
                "name": "ТРЦ Карасай Плаза, Каскелен",
                "company": "Tansu Construction",
                "manager": "samat",
                "type": "private",
                "status": "proposal_sent",
                "contract": Decimal('78920000.00'),
                "cost": Decimal('64710000.00'),
                "margin": Decimal('18.00'),
                "paid": Decimal('0.00'),
                "due": Decimal('78920000.00'),
                "equipment": "БМК 1 МВт + БТП + НС",
                "priority": "A+"
            },
            {
                "name": "Тендеры КНС (4 лота)",
                "company": "ГКП Алматы Су",
                "manager": "turar",
                "type": "quasi_state",
                "status": "qualification",
                "contract": Decimal('300000000.00'),
                "cost": Decimal('240000000.00'),
                "margin": Decimal('20.00'),
                "paid": Decimal('0.00'),
                "due": Decimal('300000000.00'),
                "equipment": "Насосы Flygt/Wilo + Schneider",
                "priority": "A+++"
            }
        ]

        projects = {}
        for pd in projects_data:
            p, _ = Project.objects.get_or_create(
                name=pd["name"],
                defaults={
                    "company": companies.get(pd["company"]),
                    "manager": managers.get(pd["manager"]),
                    "project_type": pd["type"],
                    "status": pd["status"],
                    "contract_amount": pd["contract"],
                    "cost_amount": pd["cost"],
                    "target_margin_percent": pd["margin"],
                    "paid_amount": pd["paid"],
                    "due_amount": pd["due"],
                    "equipment_type": pd["equipment"],
                    "priority": pd["priority"]
                }
            )
            projects[pd["name"]] = p

        # 4. Создаем Обещания и Дедлайны (Commitments)
        today = timezone.now().date()
        commitments_data = [
            {
                "text": "Top Build: отгрузить всё доступное оборудование Danfoss со склада в г. Семей",
                "manager": "ulugbek",
                "project": "ЦТП 343 квартал, г. Семей",
                "deadline": today - timedelta(days=5),
                "status": "fulfilled",
                "severity": "critical"
            },
            {
                "text": "Integra: устранить замечания по водомерному узлу ХВС и БТП до 18:00",
                "manager": "vyacheslav",
                "project": None,
                "deadline": today - timedelta(days=6),
                "status": "fulfilled",
                "severity": "critical"
            },
            {
                "text": "Квалификация 6 заводов (Coca-Cola, HOWO, VOLTREX)",
                "manager": "turar",
                "project": None,
                "deadline": today - timedelta(days=15),
                "status": "overdue",
                "severity": "critical"
            },
            {
                "text": "Подписание договора и передача компоновки по ЖК Казына Парк (2 МВт)",
                "manager": "kamil",
                "project": None,
                "deadline": today + timedelta(days=1),
                "status": "pending",
                "severity": "medium"
            },
            {
                "text": "Участие в тендерах Алматы Су по 4 лотам насосов на 300 млн ₸",
                "manager": "turar",
                "project": "Тендеры КНС (4 лота)",
                "deadline": date(2026, 9, 24),
                "status": "pending",
                "severity": "critical"
            },
            {
                "text": "Распределение 23 ТРЦ и 33 промышленных объектов между менеджерами",
                "manager": "vyacheslav",
                "project": None,
                "deadline": date(2026, 9, 27),
                "status": "pending",
                "severity": "critical"
            },
            {
                "text": "Karasay Plaza: согласовать с собственником Олжасом 4-трубную котельную",
                "manager": "samat",
                "project": "ТРЦ Карасай Плаза, Каскелен",
                "deadline": today + timedelta(days=3),
                "status": "pending",
                "severity": "medium"
            }
        ]

        for cd in commitments_data:
            Commitment.objects.get_or_create(
                commitment_text=cd["text"],
                defaults={
                    "manager": managers.get(cd["manager"]),
                    "project": projects.get(cd["project"]) if cd["project"] else None,
                    "deadline": cd["deadline"],
                    "status": cd["status"],
                    "severity": cd["severity"]
                }
            )

        # 5. Создаем Финансовые записи оплат
        financials = [
            ("ЦТП 343 квартал, г. Семей", Decimal('117000000.00'), today - timedelta(days=15), "milestone", "received"),
            ("ЖК Vertex Garden — котельная 10 МВт", Decimal('46800000.00'), today - timedelta(days=9), "advance", "received"),
            ("ПСЭМ-01-2026 (190 Гкал)", Decimal('285000000.00'), today - timedelta(days=35), "milestone", "received"),
            ("ПСЭМ-02-2026 (100 Гкал)", Decimal('109980000.00'), today - timedelta(days=35), "milestone", "received"),
        ]
        for proj_name, amt, pdate, ptype, pstatus in financials:
            if proj_name in projects:
                FinancialRecord.objects.get_or_create(
                    project=projects[proj_name],
                    amount=amt,
                    payment_date=pdate,
                    defaults={"payment_type": ptype, "status": pstatus}
                )

        # 6. Векторизуем ключевые сырые сообщения в Qdrant
        raw_msgs = [
            ("msg-101", "Top Build оплатили 117 млн тенге, поступили на счет компании", "Улугбек"),
            ("msg-102", "По ПСЭМ 190 Гкал и 100 Гкал маржа 14.0-14.4%. На объемах 2.3 млрд тенге каждый 1% маржи критичен", "Марат"),
            ("msg-103", "По Интегре все работы закончены, изоляцию доделают сегодня до 18:00", "Вячеслав Медведев"),
            ("msg-104", "По ЖК Курмет согласовали бартер 60/40, квартира 15 млн тенге", "Камиль"),
            ("msg-105", "По Алматы Су 4 лота на 300 млн тенге, насосы Flygt/KSB заменяем на Wilo", "Турар"),
            ("msg-106", "Алтын Сити 3 очередь: сумма 110.9 млн, прибыль 51.37 млн, маржа 46.3%", "Марат"),
        ]

        qdrant_service.ensure_collection()
        for mid, text, sender in raw_msgs:
            rm, _ = RawMessage.objects.get_or_create(
                message_id=mid,
                defaults={
                    "chat_id": "aquakip-sales-group",
                    "sender_name": sender,
                    "timestamp": timezone.now(),
                    "content": text,
                    "processed": True
                }
            )
            point_id = qdrant_service.upsert_message(mid, text, {"sender": sender})
            if point_id:
                rm.qdrant_point_id = point_id
                rm.save(update_fields=['qdrant_point_id'])

        self.stdout.write(self.style.SUCCESS("✓ Успешно посеяны реальные данные Aqua Kip Engineering!"))
