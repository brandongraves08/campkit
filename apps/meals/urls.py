from django.urls import path

from apps.meals import views

app_name = "meals"
urlpatterns = [
    path("trips/<uuid:trip_id>/", views.meal_list, name="meal_list"),
    path("trips/<uuid:trip_id>/confirm/", views.meal_plan_confirm, name="confirm_meal_plan"),
    path("trips/<uuid:trip_id>/reconcile/", views.trip_reconcile, name="reconcile_trip_food"),
]
