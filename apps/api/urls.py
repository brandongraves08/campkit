"""URLs for the additive, versioned household API."""

from django.urls import path

from apps.api import views

app_name = "api"

urlpatterns = [
    path("trips/", views.trip_collection, name="trip-collection"),
    path("trips/<uuid:trip_id>/", views.trip_detail, name="trip-detail"),
    path("trips/<uuid:trip_id>/meals/", views.trip_meal_plan, name="trip-meal-plan"),
    path("trips/<uuid:trip_id>/meals/confirm/", views.trip_meal_confirm, name="trip-meal-confirm"),
    path("gear/", views.gear_collection, name="gear-collection"),
    path("gear/<uuid:item_id>/", views.gear_detail, name="gear-detail"),
    path("gear/<uuid:item_id>/confirm/", views.gear_confirm, name="gear-confirm"),
    path("pantry/", views.pantry_collection, name="pantry-collection"),
    path("pantry/<uuid:item_id>/", views.pantry_detail, name="pantry-detail"),
    path("pantry/<uuid:item_id>/confirm/", views.pantry_confirm, name="pantry-confirm"),
    path("pantry/<uuid:item_id>/prices/", views.pantry_prices, name="pantry-prices"),
    path("pantry/<uuid:item_id>/nutrition/", views.pantry_nutrition, name="pantry-nutrition"),
    path("intake/", views.intake_collection, name="intake-collection"),
    path("intake/<uuid:submission_id>/", views.intake_detail, name="intake-detail"),
    path("intake/<uuid:submission_id>/review/", views.intake_review, name="intake-review"),
    path("verification-requests/", views.verification_request_collection, name="verification-requests"),
]
