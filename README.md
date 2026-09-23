# Mazory — Your AI Business OS (Docker & Docker Compose)

Корпоративный ИИ-ассистент и аналитический дашборд для коммерческой команды компании.

Вся разработка и отладка (debug) полностью изолированы в **Docker** контейнерах через **Docker Compose**, без необходимости локальной установки Python или Node.js.

---

## Быстрый запуск в Docker

Все команды выполняются из директории `mazory/`:

```bash
cd mazory

# Сборка и запуск всех сервисов в фоне
docker compose up -d

# Или с логами в реальном времени
docker compose up
```

### Доступ к сервисам
- **Frontend (Vue 3 + Vite HMR)**: [http://localhost:5173](http://localhost:5173)
- **Backend API (Django REST Framework)**: [http://localhost:8000/api/kpi/summary/](http://localhost:8000/api/kpi/summary/)
- **Django Admin**: [http://localhost:8000/admin/](http://localhost:8000/admin/)

---

## Режим отладки (Debugging)

### 1. Просмотр логов
```bash
# Логи обоих сервисов
docker compose logs -f

# Логи только бэкенда
docker compose logs -f backend

# Логи только фронтенда
docker compose logs -f frontend
```

### 2. Точки останова (PDB / IPDB / Breakpoints в Django)
В `docker-compose.yml` включены флаги `stdin_open: true` и `tty: true`.
Если в коде Django вы ставите `breakpoint()` или `import pdb; pdb.set_trace()`, просто подключитесь к интерактивной консоли контейнера:
```bash
docker attach mazory-backend
```
*(Для отключения без остановки контейнера: комбинация `Ctrl + P`, затем `Ctrl + Q`)*

### 3. Выполнение команд Django (миграции, создание суперпользователя, shell)
```bash
# Создание миграций
docker compose exec backend python manage.py makemigrations

# Применение миграций
docker compose exec backend python manage.py migrate

# Django Shell
docker compose exec backend python manage.py shell

# Создание суперпользователя
docker compose exec -it backend python manage.py createsuperuser
```

### 4. Горячая перезагрузка (Hot Reload)
- **Frontend**: Любые изменения в `frontend/src/` мгновенно подхватываются через Vite HMR (том `./frontend:/app`).
- **Backend**: Любые изменения в `backend/` автоматически перезагружают Django dev-сервер (через StatReloader и том `./backend:/app`).

---

## Архитектура контейнеров

```
mazory/
├── docker-compose.yml       # Оркестрация frontend и backend
├── frontend/
│   ├── Dockerfile           # Node 22 Alpine, hot reload на порту 5173
│   ├── .dockerignore
│   └── src/                 # Исходный код Vue 3
│       ├── components/      # WaveBackground (Canvas 2D), Header, ChatInput, KpiDashboardView
│       └── composables/     # useChat.ts
└── backend/
    ├── Dockerfile           # Python 3.12 Slim, Django 6.1, DRF
    ├── .dockerignore
    ├── requirements.txt
    ├── manage.py
    ├── mazory_backend/      # Настройки Django с включенным CORS
    └── api/                 # Эндпоинты /api/kpi/summary/ и /api/chat/query/
```
