from django.apps import AppConfig


class ApiConfig(AppConfig):
    """Household-scoped REST/MCP access to CampKit's domain services."""

    name = "apps.api"
    verbose_name = "Household API"
    default_auto_field = "django.db.models.BigAutoField"
