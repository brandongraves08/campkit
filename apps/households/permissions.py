from uuid import UUID

from django.core.exceptions import PermissionDenied

from apps.households.models import Membership, MembershipRole


def current_household(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise PermissionDenied("Authentication is required.")
    memberships = list(Membership.objects.filter(user=user).select_related("household"))
    if not memberships:
        raise PermissionDenied("A household membership is required.")
    if len(memberships) == 1:
        return memberships[0].household

    session = getattr(request, "session", {}) or {}
    active_household_id = session.get("active_household_id")
    try:
        active_household_id = UUID(str(active_household_id))
    except (AttributeError, TypeError, ValueError):
        raise PermissionDenied("Select one of your household memberships.")

    for membership in memberships:
        if membership.household_id == active_household_id:
            return membership.household
    raise PermissionDenied("Select one of your household memberships.")


def require_household_member(user, household_id):
    if user is None or not user.is_authenticated:
        raise PermissionDenied("Authentication is required.")
    membership = (
        Membership.objects.filter(user=user, household_id=household_id)
        .select_related("household")
        .first()
    )
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
