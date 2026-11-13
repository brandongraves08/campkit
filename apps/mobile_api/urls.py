from django.urls import path

from apps.mobile_api import views

app_name = "mobile_api"

urlpatterns = [
    path("households/", views.households, name="households"),
    path("households/<str:household_id>/trips/", views.household_trips, name="household_trips"),
    path(
        "households/<str:household_id>/trips/<str:trip_id>/packing/",
        views.trip_packing,
        name="trip_packing",
    ),
    path(
        "households/<str:household_id>/trips/<str:trip_id>/packing/<int:entry_id>/",
        views.packing_sync,
        name="packing_sync",
    ),
]
