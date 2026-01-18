from django.db import models


class Title(models.Model):
    title = models.CharField(max_length=50)

    def __str__(self):
        return self.title


class StoryPage(models.Model):
    title = models.ForeignKey(Title, on_delete=models.CASCADE, related_name='pages')
    page_number = models.IntegerField()
    content = models.TextField()
    drawing = models.TextField(blank=True, null=True)  # Stores drawing as base64 image data
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['page_number']

    def __str__(self):
        return f"{self.title.title} - Page {self.page_number}"

