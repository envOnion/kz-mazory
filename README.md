# Mazory — AI-Driven Sales OS & Analytics Platform

Корпоративная операционная система управления продажами и аналитики для коммерческой команды **Aqua Kip Engineering**.

Интегрирует потоковые сообщения WhatsApp (через локальный шлюз **WAHA**), векторную базу **Qdrant**, аналитические витрины данных **Data Mart**, нейросетевые модели OpenRouter (**LFM-2.5 1024d Embeddings** и **Nemotron-3-Ultra 550b Chat**) и CRM-систему **Bitrix24**.

---

## 🏗 Архитектура системы

- **Единая внешняя точка входа (Nginx Gateway)**:
  - Публичные порты: `80` (HTTP) и `443` (HTTPS с автопродлением Let's Encrypt через Certbot).
  - Единый домен для Vue 3 фронтенда (`/`), REST API (`/api/`) и Django Unfold Admin (`/admin/`).
  - Все остальные сервисы (`postgres`, `redis`, `qdrant`, `waha`, `backend`, `qcluster`) изолированы во внутренней сети `mazory-network`.
- **Интерфейс пользователя (Frontend)**: Vue 3 + Tailwind CSS + Lucide Icons + Canvas 2D Wave Background + Chart.js.
  - Полностью исключены mock-данные — все показатели загружаются из живых эндпоинтов `/api/kpi/summary/`, `/api/chat/query/`, `/api/profile/`, `/api/projects/`.
- **Django Unfold Admin**:
  - Современная темная тема админки Unfold с фирменной символикой Mazory (логотип, favicon, брендированный сайдбар).
  - **Центр управления WAHA (`/admin/waha-dashboard/`)**: мониторинг сессии, вывод актуального QR-кода (base64) для авторизации смартфона и просмотр списка чатов.
  - Настройка WhatsApp группы (`WhatsAppConfig`): редактирование JID группы (`120363024823904923@g.us`) в базе PostgreSQL без правок `.env`.
  - Настройка нейросетей (`AISettings`): управление моделями embeddings и reasoning, системными промптами и API-ключами.
  - Настройка Bitrix24 (`BitrixSettings`): вебхук, авто-создание сделок и аудит синхронизации.
- **Фоновый конвейер (Django Q2 Worker)**:
  - Прием сообщений из WhatsApp через вебхук WAHA (`/api/whatsapp/webhook/`).
  - Фильтрация по JID группы из `WhatsAppConfig`.
  - Плотная векторизация через OpenRouter `liquid/lfm-2.5-embedding-350m:free` (1024-мерный вектор) и сохранение в Qdrant (`mazory_messages`).
  - Семантический RAG-поиск по истории переписки и контексту существующих объектов.
  - Извлечение сущностей через LLM `nvidia/nemotron-3-ultra-550b-a55b:free`.
  - Автоматическая регистрация/обновление сделок в PostgreSQL (`Project`) и в **Bitrix24 CRM** (`BitrixService.create_deal`).
  - Фиксация обязательств (`Commitment`), платежей (`FinancialRecord`) и отправка уведомлений.

---

## 🚀 Быстрый запуск

### 1. Запуск через Docker Compose

```bash
cd mazory

# Сборка и фоновый запуск всех сервисов
docker compose up -d

# Проверка статуса контейнеров
docker compose ps
```

### 2. Доступ к интерфейсам
- **Веб-интерфейс Mazory**: [http://localhost](http://localhost) (или ваш домен)
- **Django Unfold Admin**: [http://localhost/admin/](http://localhost/admin/)
- **Центр авторизации WAHA**: [http://localhost/admin/waha-dashboard/](http://localhost/admin/waha-dashboard/)
- **REST API**: [http://localhost/api/](http://localhost/api/)

---

## 🔒 Настройка SSL через Certbot

Для включения HTTPS на вашем сервере (например, `crm.aquakip.kz`):

```bash
# Выполните скрипт авто-настройки:
./scripts/setup_ssl.sh <ваш-домен> <ваш-email>

# Пример:
./scripts/setup_ssl.sh crm.aquakip.kz admin@aquakip.kz
```

Скрипт автоматически:
1. Запустит Nginx на порту 80 для прохождения ACME challenge.
2. Запросит бесплатный SSL-сертификат у Let's Encrypt.
3. Активирует защищенный конфигурационный файл Nginx с перенаправлением с HTTP на HTTPS (порт 443).
4. Настроит автопродление сертификата каждые 12 часов.

---

## 📦 Разовая выгрузка и сидирование данных из Bitrix24

В корне проекта расположен скрипт `seed_bitrix_data.py`. Он выгружает из CRM Bitrix24 пользователей, компании, сделки (`crm.deal`) и связанные смарт-процессы (договора, расчеты, платежи) и наполняет базу данных PostgreSQL.

### Тестовый прогон (Dry Run):
```bash
# Из папки backend:
uv run python ../seed_bitrix_data.py --dry-run
```

### Боевой импорт в PostgreSQL:
```bash
# Из папки backend:
uv run python ../seed_bitrix_data.py

# Или внутри контейнера backend:
docker compose exec backend python ../seed_bitrix_data.py
```

Скрипт автоматически определяет окружение (хост или докер-контейнер) и подключается к порту PostgreSQL `5434` на хосте либо `5432` внутри сети.

---

## ⚙️ Управление и отладка

### Применение миграций и сбор статики:
```bash
# Миграции
docker compose exec backend python manage.py migrate

# Сбор статических файлов темы Unfold
docker compose exec backend python manage.py collectstatic --noinput
```

### Создание суперпользователя:
```bash
docker compose exec -it backend python manage.py createsuperuser
```

### Просмотр логов сервисов:
```bash
# Логи бэкенда и фонового воркера
docker compose logs -f backend qcluster

# Логи шлюза WAHA
docker compose logs -f waha

# Логи Nginx
docker compose logs -f nginx
```

---

## 🧪 Безопасность при тестировании

Интеграция с Bitrix24 защищена правилом безопасности:
- При любых end-to-end тестах создаваемые тестовые сделки именуются с префиксом `[TEST_MAZORY_AUTO_DELETE]`.
- После проверки они **немедленно удаляются** методом `BitrixService.delete_deal(deal_id)`, чтобы не загрязнять боевую CRM заказчика.
