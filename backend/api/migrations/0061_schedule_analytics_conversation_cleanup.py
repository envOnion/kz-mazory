from django.db import migrations
from django.utils import timezone


SCHEDULE_NAME = "cleanup-expired-analytics-conversations"
TASK_PATH = "api.analytics.dialogue.cleanup_expired_conversations"


def schedule_cleanup(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.using(schema_editor.connection.alias).get_or_create(
        name=SCHEDULE_NAME,
        defaults={
            "func": TASK_PATH,
            "schedule_type": "D",
            "repeats": -1,
            "next_run": timezone.now(),
            "cluster": "mazory_q",
        },
    )


def remove_cleanup_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.using(schema_editor.connection.alias).filter(name=SCHEDULE_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0060_monotonic_record_updates"),
        ("django_q", "0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more"),
    ]

    operations = [migrations.RunPython(schedule_cleanup, remove_cleanup_schedule)]
