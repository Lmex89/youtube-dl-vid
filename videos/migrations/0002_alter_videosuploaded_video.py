# Generated by Django 3.2 on 2023-05-29 23:46

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='videosuploaded',
            name='video',
            field=models.FileField(blank=True, null=True, upload_to='static/videos/'),
        ),
    ]