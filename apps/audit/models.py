import uuid

from django.conf import settings
from django.db import models



class InventoryChange(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    household = models.ForeignKey("households.Household", on_delete=models.CASCADE, related_name="inventory_changes")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="inventory_changes")
    object_type = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=80, blank=True)
    action = models.CharField(max_length=40)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("household", "-created_at"), name="audit_household_time_idx")]

    def __str__(self):
        return f"{self.action} {self.object_type} {self.object_id}"
