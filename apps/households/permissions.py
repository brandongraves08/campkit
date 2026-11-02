from django.core.exceptions import PermissionDenied

from apps.households.models import Household, Membership, MembershipRole


def current_household(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise PermissionDenied("Authentication is required.")
    memberships = Membership.objects.filter(user=user).select_related("household")
    if memberships.count() != 1:
        raise PermissionDenied("A single unambiguous household membership is required.")
    return memberships.get().household


def require_household_member(user, household_id):
    if user is None or not user.is_authenticated:
        raise PermissionDenied("Authentication is required.")
    membership = Membership.objects.filter(user=user, household_id=household_id).select_related("household").first()
    if membership is None:
        raise PermissionDenied("You are not a member of this household.")
    return membership.household


def require_household_owner(user, household_id):
    household = require_household_member(user, household_id)
    if not Membership.objects.filter(
        user=user, household=household, role=MembershipRole.OWNER
    ).exists():
        raise PermissionDenied("Only household owners may perform this action.")
    return household
