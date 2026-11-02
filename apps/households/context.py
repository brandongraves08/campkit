from django.core.exceptions import PermissionDenied

from apps.households.permissions import current_household
from apps.households.services import household_role


def household_context(request):
    """Expose the household scope on every rendered page (header, nav, tests).

    A visitor with no single unambiguous membership gets an empty mapping rather
    than a 500; the base template then renders anonymous chrome.
    """
    try:
        household = current_household(request)
    except PermissionDenied:
        return {}
    return {
        "household": household,
        "household_role": household_role(actor=request.user, household=household),
    }
