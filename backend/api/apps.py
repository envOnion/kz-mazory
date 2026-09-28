from django.apps import AppConfig


class ApiConfig(AppConfig):
    name = "api"

    def ready(self):
        # Import checks only; management commands never start network tasks.
        from . import checks  # noqa: F401
