import json
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from api.authentication import create_session


class Command(BaseCommand):
    help = "Create a session only in an explicitly isolated E2E database."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        if not settings.INTEGRATION_TEST_MODE or not str(
            settings.DATABASES["default"]["NAME"]
        ).endswith("_e2e"):
            raise CommandError(
                "Requires INTEGRATION_TEST_MODE and a database ending in _e2e"
            )
        user = User.objects.get(username=options["username"])
        session, access, refresh = create_session(user)
        self.stdout.write(
            json.dumps(
                {
                    "access": access,
                    "refresh": refresh,
                    "user_id": user.id,
                    "session_id": session.id,
                }
            )
        )
