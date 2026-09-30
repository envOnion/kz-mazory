# Mazory — AI-Driven Sales OS & Analytics Platform

Корпоративная операционная система управления продажами и аналитики для коммерческой команды **Aqua Kip Engineering**.

Интегрирует потоковые сообщения WhatsApp (через локальный шлюз **WAHA**), векторную базу **Qdrant**, аналитические витрины данных **Data Mart**, нейросетевые модели OpenRouter (**LFM-2.5 1024d Embeddings** и **Nemotron-3-Ultra 550b Chat**) и CRM-систему **Bitrix24**.

---

## 🏗 Архитектура системы

- **Внешняя TLS-граница**: системный nginx на production-сервере принимает публичный HTTP/HTTPS и завершает TLS. Он не входит в Docker Compose этого репозитория и не изменяется при деплое Mazory.
- **Единый Docker gateway**: сервис `nginx` публикует только `127.0.0.1:18080` на host и принимает от системного nginx обычный HTTP. Он раздаёт Vue-сборку и Django static, а `/api/` и `/admin/` проксирует на `backend:8000`.
- **Интерфейс пользователя (Frontend)**: Vue 3 + Tailwind CSS + Lucide Icons + Canvas 2D Wave Background + Chart.js. Node.js и Vite используются только на build-stage; готовый `dist` встроен в image gateway. Runtime-сервиса `frontend` в Compose нет.
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

```text
Internet :80/:443
  → системный nginx (TLS, вне Compose)
  → 127.0.0.1:18080
  → Docker nginx gateway :80
      ├─ Vue dist и Django static
      └─ backend:8000 (/api/, /admin/)
```

Остальные сервисы (`backend`, очереди, `postgres`, `redis`, `qdrant`, `waha`) не публикуют production-порты во внешнюю сеть. В Compose также нет runtime-сервиса Certbot: сертификатами управляет внешний TLS-edge.

---

## 🚀 Быстрый запуск

### 1. Production deployment

```bash
cd /path/to/mazory-release

# Использовать production override с существующими volumes и Docker-сетью.
# Защищённый .env должен находиться в release-каталоге и не храниться в Git.
docker compose -f docker-compose.yml -f compose.server.yml config --quiet
docker compose -f docker-compose.yml -f compose.server.yml build nginx

# Проверить новый image до переключения на отдельном loopback-порту.
MAZORY_PREFLIGHT_PORT=18082 # выберите свободный loopback-порт на сервере
MAZORY_GATEWAY_IMAGE=$(docker compose -f docker-compose.yml -f compose.server.yml config --format json \
  | jq -er '.services.nginx.image')
docker image inspect "$MAZORY_GATEWAY_IMAGE" >/dev/null
docker run -d --rm --name mazory-nginx-preflight \
  --network mazory_mazory-network \
  -p "127.0.0.1:${MAZORY_PREFLIGHT_PORT}:80" \
  -v mazory_static_files:/staticfiles:ro \
  "$MAZORY_GATEWAY_IMAGE"
curl --fail "http://127.0.0.1:${MAZORY_PREFLIGHT_PORT}/api/health/ready/"
curl --head "http://127.0.0.1:${MAZORY_PREFLIGHT_PORT}/admin"
docker stop mazory-nginx-preflight

# Только после успешного preflight точечно заменить gateway и удалить старый
# runtime frontend, не пересоздавая backend, workers и stateful-сервисы.
docker compose -f docker-compose.yml -f compose.server.yml up -d --no-deps --remove-orphans nginx

# При изменении Django static заполнить общий volume перед smoke-check.
docker compose -f docker-compose.yml -f compose.server.yml exec backend python manage.py collectstatic --noinput

# Проверка статуса контейнеров
docker compose -f docker-compose.yml -f compose.server.yml ps
```

Для удалённой проверки контейнеров без смены глобального context используйте `docker --context mazory ps`. Compose deployment выполняется из подготовленного release-каталога на сервере, чтобы защищённый `.env` не копировался в рабочую машину.

Перед переключением сохраните путь предыдущего release. Если smoke-check нового gateway не прошёл, верните прежние два Docker-сервиса из него; эта команда не затрагивает system nginx и stateful-сервисы:

```bash
cd /path/to/previous-mazory-release
docker compose -f docker-compose.yml -f compose.server.yml up -d --no-deps nginx frontend
```

### 2. Доступ к интерфейсам

- **Веб-интерфейс Mazory**: [https://ai.mazory.best/](https://ai.mazory.best/)
- **Django Unfold Admin**: [https://ai.mazory.best/admin/](https://ai.mazory.best/admin/)
- **Центр авторизации WAHA**: [https://ai.mazory.best/admin/waha-dashboard/](https://ai.mazory.best/admin/waha-dashboard/)
- **REST API**: [https://ai.mazory.best/api/](https://ai.mazory.best/api/)

---

## 🔒 TLS и сетевая граница

TLS-сертификаты и редирект HTTP → HTTPS обслуживает системный nginx. Docker gateway работает только по HTTP и доступен только на loopback-адресе `127.0.0.1:18080`. Не публикуйте этот порт на `0.0.0.0` и не добавляйте TLS/Certbot в Compose.

Конфигурация системного nginx, другие домены сервера и host-level Certbot находятся вне границ этого репозитория.

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
# Снимок логов основного контура
docker compose logs --tail=50 nginx backend qcluster qcluster_delivery qcluster_history qcluster_crm outbox waha

# Поток логов gateway, backend и WAHA
docker compose logs -f nginx backend waha
```

---

## 🧪 Безопасность при тестировании

Интеграция с Bitrix24 защищена правилом безопасности:
- При любых end-to-end тестах создаваемые тестовые сделки именуются с префиксом `[TEST_MAZORY_AUTO_DELETE]`.
- После проверки они **немедленно удаляются** методом `BitrixService.delete_deal(deal_id)`, чтобы не загрязнять боевую CRM заказчика.
