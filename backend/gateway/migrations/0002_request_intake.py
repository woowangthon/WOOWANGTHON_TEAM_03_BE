from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("gateway", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="request",
            name="intake_fingerprint",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
        migrations.AlterField(
            model_name="document",
            name="extraction_status",
            field=models.CharField(
                choices=[
                    ("uploading", "업로드 중"),
                    ("pending", "텍스트 추출 대기"),
                    ("extracting", "텍스트 추출 중"),
                    ("ready", "추출 완료"),
                    ("failed", "업로드 또는 추출 실패"),
                ],
                default="uploading", max_length=10,
            ),
        ),
    ]
