from django.conf import settings
from django.db import models


class PackingOperation(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mobile_packing_operations")
    operation_id = models.UUIDField()
    household = models.ForeignKey("households.Household", on_delete=models.CASCADE, related_name="mobile_packing_operations")
    trip_id = models.UUIDField()
    entry_id = models.PositiveBigIntegerField()
    request_hash = models.CharField(max_length=64)
    result = models.JSONField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("actor", "operation_id"), name="unique_mobile_packing_actor_operation"),
        ]
