from django.core.management.base import BaseCommand
from django_q.tasks import async_task


class Command(BaseCommand):
    help = 'Восстановить журнал из доставок и чтения Bitrix без повторной отправки.'

    def handle(self, *args, **options):
        self.stdout.write(async_task('api.tasks.rebuild_bitrix_journal', cluster='history',
                                     task_name='bitrix-journal-history-readback'))
