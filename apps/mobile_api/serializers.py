from apps.trips.models import TripGear


def household_dict(household):
    return {"id": str(household.pk), "name": household.name}


def trip_dict(trip):
    return {
        "id": str(trip.pk),
        "name": trip.name,
        "destination": trip.destination,
        "start_date": trip.start_date.isoformat() if trip.start_date else None,
        "end_date": trip.end_date.isoformat() if trip.end_date else None,
        "party_size": trip.party_size,
        "conditions": trip.conditions,
        "status": trip.status,
    }


def packing_snapshot(*, trip, household):
    entries = (
        TripGear.objects.filter(trip=trip, gear_item__household=household)
        .select_related("gear_item")
        .order_by("pk")
    )
    return {
        "trip": {"id": str(trip.pk), "name": trip.name},
        "packing": [
            {
                "id": entry.pk,
                "packed": entry.packed,
                "quantity": entry.quantity,
                "version": entry.version,
                "gear": {
                    "id": str(entry.gear_item.pk),
                    "name": entry.gear_item.name,
                    "category": entry.gear_item.category,
                },
            }
            for entry in entries
        ],
    }
