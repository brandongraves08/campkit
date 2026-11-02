from django.urls import path

from apps.inventory import views

app_name = "inventory"
urlpatterns = [
    path("", views.gear_list, name="gear_list"),
    path("gear/new/", views.gear_create, name="gear_create"),
    path("gear/<uuid:item_id>/edit/", views.gear_edit, name="gear_edit"),
    path("gear/<uuid:item_id>/delete/", views.gear_delete, name="gear_delete"),
    path("gear/<uuid:item_id>/confirm/", views.gear_confirm, name="gear_confirm"),
    path("pantry/", views.pantry_list, name="pantry_list"),
    path("pantry/new/", views.pantry_create, name="pantry_create"),
    path("pantry/<uuid:item_id>/edit/", views.pantry_edit, name="pantry_edit"),
    path("pantry/<uuid:item_id>/delete/", views.pantry_delete, name="pantry_delete"),
    path("pantry/<uuid:item_id>/confirm/", views.pantry_confirm, name="pantry_confirm"),
]
