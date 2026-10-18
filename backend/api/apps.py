from django.apps import AppConfig


class ApiConfig(AppConfig):
    name = "api"
    verbose_name = "Служебные данные Mazory"

    def ready(self):
        # Import checks only; management commands never start network tasks.
        from . import checks  # noqa: F401
        # Registers the communication journal signals (idempotent projections).
        from . import communication  # noqa: F401
