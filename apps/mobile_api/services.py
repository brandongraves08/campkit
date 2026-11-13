from django.core.exceptions import ValidationError

from apps.households.models import Household
from apps.trips.models import Trip
from apps.trips.services import list_trips


def member_households(*, member):
    return Household.objects.filter(memberships__user=member).order_by("name", "pk")


def active_household_for_member(*, household_id, member):
    """Resolve requested household only when the member has active membership."""
    if not getattr(member, "is_authenticated", False):
        return None
    try:
        return Household.objects.filter(memberships__user=member).get(pk=household_id)
    except (Household.DoesNotExist, TypeError, ValueError, ValidationError):
        return None


def trips_for_member_household(*, member, household):
    return list_trips(actor=member, household=household)


def trip_for_member_household(*, trip_id, household):
    """Resolve a trip only inside a previously membership-authorized household."""
    try:
        return Trip.objects.for_household(household).get(pk=trip_id)
    except (Trip.DoesNotExist, TypeError, ValueError, ValidationError):
        return None
