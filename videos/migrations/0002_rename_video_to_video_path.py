from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("videos", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="videosuploaded",
            old_name="video",
            new_name="video_path",
        ),
        migrations.AlterField(
            model_name="videosuploaded",
            name="video_path",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
    ]
