"""Keep cursor ordering stable when application and database clocks differ."""
from django.db import migrations
from ._temporal_v1 import TIMESTAMPS

MONOTONIC_TIMESTAMPS = TIMESTAMPS.replace(
    'NEW.updated_at := clock_timestamp();',
    "NEW.updated_at := GREATEST(clock_timestamp(), OLD.updated_at + interval '1 microsecond');",
)


class Migration(migrations.Migration):
    dependencies = [('api', '0059_alter_adminmfa_options_and_more')]
    operations = [migrations.RunSQL(MONOTONIC_TIMESTAMPS, reverse_sql=TIMESTAMPS)]
