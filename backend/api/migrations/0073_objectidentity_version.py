from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("api", "0072_aisettings_entity_input_token_limit_and_more")]

    operations = [migrations.AddField(
        model_name="objectidentity", name="version", field=models.PositiveIntegerField(default=1),
    )]
