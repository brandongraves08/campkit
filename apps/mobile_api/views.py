from django.http import JsonResponse
from django.views.decorators.http import require_GET

from apps.mobile_api.permissions import authenticate_mobile, not_found
from apps.mobile_api.serializers import household_dict, packing_snapshot, trip_dict
from apps.mobile_api.services import (
    active_household_for_member,
    member_households,
    trip_for_member_household,
    trips_for_member_household,
)


@require_GET
@authenticate_mobile
def households(request):
    rows = member_households(member=request.user)
    return JsonResponse({"households": [household_dict(row) for row in rows]})


@require_GET
@authenticate_mobile
def household_trips(request, household_id):
    household = active_household_for_member(household_id=household_id, member=request.user)
    if household is None:
        return not_found()
    # Keep the established membership-checked trip service as the query boundary.
    trips = trips_for_member_household(member=request.user, household=household)
    return JsonResponse({"trips": [trip_dict(trip) for trip in trips]})


@require_GET
@authenticate_mobile
def trip_packing(request, household_id, trip_id):
    household = active_household_for_member(household_id=household_id, member=request.user)
    if household is None:
        return not_found()
    # Do not query trips until the requested household membership is proven.
    trip = trip_for_member_household(trip_id=trip_id, household=household)
    if trip is None:
        return not_found()
    return JsonResponse(packing_snapshot(trip=trip, household=household))
