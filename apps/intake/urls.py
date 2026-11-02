from django.urls import path

from apps.intake import views

app_name = "intake"
urlpatterns = [
    path("", views.intake_list, name="list"),
    path("new/", views.intake_create, name="create"),
    path("<uuid:submission_id>/", views.intake_detail, name="detail"),
    path("<uuid:submission_id>/image/", views.intake_image, name="image"),
    path("<uuid:submission_id>/resolve/", views.intake_resolve, name="resolve"),
]
