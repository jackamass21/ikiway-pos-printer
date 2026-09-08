from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pos", "0011_posconfiguration_show_product_images")]
    operations = [
        migrations.AddField(
            model_name="posconfiguration",
            name="print_method",
            field=models.CharField(choices=[("browser", "Navegador"), ("agent", "Agente USB Ikiway")], default="browser", max_length=16),
        ),
    ]
