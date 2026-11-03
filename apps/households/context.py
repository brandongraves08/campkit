from django.core.exceptions import PermissionDenied

from apps.households.permissions import current_household
from apps.households.services import household_role


def household_context(request):
    """Expose the active household and the authenticated user's choices."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}

    memberships = list(
        request.user.memberships.select_related("household").order_by("household__name")
    )
    choices = []
    try:
        household = current_household(request)
    except PermissionDenied:
        household = None

    for membership in memberships:
        choices.append(
            {
                "household": membership.household,
                "selected": household is not None
                and membership.household_id == household.pk,
            }
        )

    context = {"household_choices": choices}
    if household is not None:
        context["household"] = household
        context["household_role"] = household_role(
            actor=request.user, household=household
        )
    return context
