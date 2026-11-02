from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from apps.households import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/signup/", views.signup_without_invitation, name="signup"),
    path("accounts/signup/<uuid:token>/", views.accept_invitation, name="accept_invitation"),
    path("accounts/invitations/", views.create_invitation, name="create_invitation"),
    path("inventory/", include("apps.inventory.urls")),
    path("trips/", include("apps.trips.urls")),
    path("meals/", include("apps.meals.urls")),
    path("intake/", include("apps.intake.urls")),
    path("api/v1/", include("apps.api.urls")),
]
