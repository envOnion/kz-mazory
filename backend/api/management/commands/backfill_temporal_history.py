from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from api.temporal import enqueue

class Command(BaseCommand):
    help='Queue bounded temporal history recovery through outbox and Q2 (administrator only).'
    def add_arguments(self,parser): parser.add_argument('--user-id',type=int,required=True)
    def handle(self,*args,**options):
        user=User.objects.get(pk=options['user_id'])
        try: op=enqueue(user)
        except PermissionError as exc: raise CommandError(str(exc)) from None
        self.stdout.write(f'Temporal backfill operation {op.id}: {op.status}')
