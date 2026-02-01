from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("storygame", "0005_lobby_participant"),
    ]

    operations = [
        migrations.AddField(
            model_name="lobby",
            name="started",
            field=models.BooleanField(default=False),
        ),
    ]
