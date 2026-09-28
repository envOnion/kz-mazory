from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Legacy command retired: use authenticated inbox and human review."

    def handle(self, *args, **options):
        raise CommandError(
            "Команда отключена: импорт и изменения финансов проходят проверку фактов. Рабочие данные не изменены."
        )
