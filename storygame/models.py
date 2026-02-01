from django.db import models
from django.contrib.auth import get_user_model


class Title(models.Model):
    title = models.CharField(max_length=50)

    def __str__(self):
        return self.title


class StoryPage(models.Model):
    title = models.ForeignKey(Title, on_delete=models.CASCADE, related_name="pages")
    page_number = models.IntegerField()
    content = models.TextField()
    drawing = models.TextField(
        blank=True, null=True
    )  # Stores drawing as base64 image data
    narration = models.TextField(
        blank=True, null=True
    )  # Stores narration audio as base64 data URL
    narration_mime = models.CharField(
        max_length=50, blank=True, null=True
    )  # Stores the mime type of the narration audio
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["page_number"]

    def __str__(self):
        return f"{self.title.title} - Page {self.page_number}"


# New multiplayer models
class Lobby(models.Model):
    # A short join code (6 chars) or slug generated when creating a lobby
    code = models.CharField(max_length=12, unique=True)
    host_name = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)
    # Mark when the host starts the game so polling clients can detect it
    started = models.BooleanField(default=False)

    def __str__(self):
        return f"Lobby {self.code} (host={self.host_name})"


class Participant(models.Model):
    ROLE_CHOICES = [
        ("unassigned", "Unassigned"),
        ("writer", "Writer"),
        ("artist", "Artist"),
        ("narrator", "Narrator"),
    ]

    lobby = models.ForeignKey(
        Lobby, on_delete=models.CASCADE, related_name="participants"
    )
    name = models.CharField(max_length=100)
    # Token to allow reconnecting to the same Participant record
    connection_token = models.CharField(
        max_length=64, blank=True, null=True, unique=True
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="unassigned")
    joined_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} @{self.lobby.code} ({self.role})"
