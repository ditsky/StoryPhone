from django.urls import path
from storygame import views

app_name = "storygame"

urlpatterns = [
    # Make the site index show the Create Lobby page
    path("", views.create_lobby, name="index"),
    path("create-title/", views.create_title, name="create_title"),
    path("success/", views.success, name="success"),
    path("add-page/", views.add_story_page, name="add_story_page"),
    path(
        "add-page/<int:title_id>/", views.add_story_page, name="add_story_page_with_id"
    ),
    path("page-success/<int:title_id>/", views.page_success, name="page_success"),
    path("stories/", views.story_list, name="story_list"),
    path("story/<int:title_id>/", views.view_story, name="view_story"),
    path("draw/<int:page_id>/", views.draw_on_page, name="draw_on_page"),
    path("draw-success/<int:page_id>/", views.draw_success, name="draw_success"),
    path("narrate/<int:page_id>/", views.narrate_page, name="narrate_page"),
    path(
        "narrate-success/<int:page_id>/", views.narrate_success, name="narrate_success"
    ),
    # Multiplayer lobbies
    path("lobby/create/", views.create_lobby, name="create_lobby"),
    path("lobby/join/", views.join_lobby, name="join_lobby"),
    path("lobby/<str:code>/", views.lobby_view, name="lobby_view"),
    path(
        "lobby/<str:code>/participants/",
        views.lobby_participants_api,
        name="lobby_participants_api",
    ),
    path("lobby/<str:code>/assign/", views.lobby_assign_api, name="lobby_assign_api"),
    path("lobby/<str:code>/start/", views.lobby_start_api, name="lobby_start_api"),
    path("lobby/<str:code>/waiting/", views.waiting_view, name="lobby_waiting"),
]
