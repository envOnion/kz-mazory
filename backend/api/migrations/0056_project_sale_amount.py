from django.db import migrations, models


# Extend the existing append-only business journal without rewriting its history.
PROJECT_FIELDS = "team_id,manager_id,company_id,status,archived,identity_confirmed,is_verified,contract_known,contract_amount,cost_amount,cost_confirmed,currency,source_created_at,source_updated_at,source_time_precision,whatsapp_fields"


class Migration(migrations.Migration):
    dependencies = [("api", "0055_bitrixdealchangelog_delivery_id_and_more")]
    operations = [
        migrations.AddField(
            model_name="project", name="sale_amount",
            field=models.DecimalField(
                "Сумма продажи", max_digits=14, decimal_places=2,
                null=True, blank=True,
                help_text="Явная цена продажи или нашего КП; не подтверждает договор или оплату.",
            ),
        ),
        migrations.RunSQL(
            "DROP TRIGGER IF EXISTS mazory_business_revision ON public.api_project; "
            "CREATE TRIGGER mazory_business_revision AFTER INSERT OR UPDATE OR DELETE ON public.api_project "
            "FOR EACH ROW EXECUTE FUNCTION public.mazory_business_revision('project','" + PROJECT_FIELDS + ",sale_amount');",
            "DROP TRIGGER IF EXISTS mazory_business_revision ON public.api_project; "
            "CREATE TRIGGER mazory_business_revision AFTER INSERT OR UPDATE OR DELETE ON public.api_project "
            "FOR EACH ROW EXECUTE FUNCTION public.mazory_business_revision('project','" + PROJECT_FIELDS + "');",
        ),
    ]
