from django.urls import path

from apps.trips import views

app_name = "trips"
urlpatterns = [
    path("", views.trip_list, name="trip_list"),
    path("<uuid:trip_id>/", views.trip_detail, name="trip_detail"),
    path("<uuid:trip_id>/gear/", views.trip_gear_add, name="trip_gear_add"),
    path("<uuid:trip_id>/gear/<int:entry_id>/packed/", views.trip_gear_packed, name="trip_gear_packed"),
    path("<uuid:trip_id>/cancel/", views.trip_cancel, name="trip_cancel"),
    path("<uuid:trip_id>/status/", views.trip_status, name="trip_status"),
]
