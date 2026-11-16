from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("videos", "0005_remove_videosuploaded_title_default")]

    operations = [
        migrations.AddField(
            model_name="codecurls",
            name="error_reason",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
