from django.core.exceptions import PermissionDenied

from apps.households.permissions import require_household_member
from apps.audit.models import InventoryChange


def _object_household(obj):
    return getattr(obj, "household", getattr(getattr(obj, "item", None), "household", None))


def record_change(*, actor, household, object, action, before, after) -> InventoryChange:
    require_household_member(actor, household.pk)
    target_household = _object_household(object) if object is not None else household
    if object is not None and (target_household is None or target_household.pk != household.pk):
        raise PermissionDenied("Audit records cannot cross household boundaries.")
    return InventoryChange.objects.create(
        actor=actor,
        household=household,
        object_type=object._meta.label_lower if object is not None else "",
        object_id=str(object.pk) if object is not None else "",
        action=action,
        before=before,
        after=after,
    )


def recent_changes(*, actor, household, limit=8):
    """Household-scoped audit rows, newest first, for the overview screen."""
    require_household_member(actor, household.pk)
    limit = max(int(limit), 0)
    return (
        InventoryChange.objects.filter(household=household)
        .select_related("actor")
        .order_by("-created_at", "-id")[:limit]
    )
