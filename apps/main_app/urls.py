from django.urls import path
from apps.main_app import views

app_name = "main_app"

urlpatterns = [
    path("create-title/", views.create_title, name="create_title"),
    path("success/", views.success, name="success"),
    path("add-page/", views.add_story_page, name="add_story_page"),
    path("add-page/<int:title_id>/", views.add_story_page, name="add_story_page_with_id"),
    path("page-success/<int:title_id>/", views.page_success, name="page_success"),
    path("stories/", views.story_list, name="story_list"),
    path("story/<int:title_id>/", views.view_story, name="view_story"),
    path("draw/<int:page_id>/", views.draw_on_page, name="draw_on_page"),
    path("draw-success/<int:page_id>/", views.draw_success, name="draw_success"),
]

