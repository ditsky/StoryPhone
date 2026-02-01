from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("storygame", "0006_lobby_started"),
    ]

    operations = [
        migrations.AddField(
            model_name="participant",
            name="connection_token",
            field=models.CharField(max_length=64, null=True, unique=True, blank=True),
        ),
    ]
