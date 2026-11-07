from django.urls import path

from apps.trips import views

app_name = "trips"
urlpatterns = [
    path("", views.trip_list, name="trip_list"),
    path("kits/", views.gear_kit_list, name="gear_kit_list"),
    path("kits/<uuid:kit_id>/edit/", views.gear_kit_edit, name="gear_kit_edit"),
    path("kits/<uuid:kit_id>/delete/", views.gear_kit_delete, name="gear_kit_delete"),
    path("<uuid:trip_id>/", views.trip_detail, name="trip_detail"),
    path("<uuid:trip_id>/save-kit/", views.gear_kit_create_from_trip, name="gear_kit_create_from_trip"),
    path("<uuid:trip_id>/gear/", views.trip_gear_add, name="trip_gear_add"),
    path("<uuid:trip_id>/gear/<int:entry_id>/packed/", views.trip_gear_packed, name="trip_gear_packed"),
    path("<uuid:trip_id>/cancel/", views.trip_cancel, name="trip_cancel"),
    path("<uuid:trip_id>/status/", views.trip_status, name="trip_status"),
]
