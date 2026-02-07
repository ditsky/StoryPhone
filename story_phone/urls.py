from django.urls import include, path

urlpatterns = [
    path("", include("apps.main_app.urls")),
]
