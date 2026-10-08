from django.db import migrations
from ._temporal_v1 import install, uninstall


class Migration(migrations.Migration):
    dependencies = [('api', '0049_temporal_timestamps')]
    operations = [migrations.RunPython(install, uninstall)]
