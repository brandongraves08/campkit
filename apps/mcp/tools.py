"""Hermes-side MCP adapters for the household API.

Only the operations the plan allows are exposed. Every handler delegates to
:mod:`apps.api.facade`, which in turn calls the same domain services as the
server-rendered UI, so no inventory, reservation, intake, or pricing rule is
reimplemented here. Destructive operations (delete, bulk edit, purchase) have no
tool at all.
"""

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError

from apps.api import facade

CONFIRM_PROPERTY = {
    "type": "boolean",
    "description": (
        "Must be true to perform this sensitive mutation. Omit it to receive a "
        "confirmation prompt describing exactly what would change."
    ),
}


def _schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _text(description):
    return {"type": "string", "description": description}


def _integer(description):
    return {"type": "integer", "description": description}


def _number(description):
    return {"type": "string", "description": description}


def _uuid(description):
    return {"type": "string", "format": "uuid", "description": description}


GEAR_PROPERTIES = {
    "name": _text("Gear item name."),
    "category": _text("Gear category for durables, one of: Backpack, Shelter, Sleep system, Cookware, Kitchen, Water, Lighting, Navigation, Tools & repair, Clothing, Climbing, Safety, Food storage, Other. Food and consumables belong in pantry, never gear."),
    "owner": _text("Who owns the item."),
    "condition": _text("Condition, for example good or worn."),
    "weight_grams": _integer("Item weight per unit in grams."),
    "quantity": _integer("How many units the household holds."),
    "storage_location": _text("Where the item is stored."),
    "availability": _text("Availability label, for example available or loaned."),
}

PANTRY_PROPERTIES = {
    "name": _text("Pantry item name."),
    "quantity": _number("Decimal quantity, for example '2.5'."),
    "unit": _text("Supported unit: g, kg, ml, l, each, oz, lb, cup, tbsp, tsp."),
    "expiry_date": {"type": "string", "format": "date", "description": "Expiry date (YYYY-MM-DD)."},
    "storage_location": _text("Where the item is stored."),
}

TRIP_PROPERTIES = {
    "name": _text("Trip name."),
    "destination": _text("Destination."),
    "start_date": {"type": "string", "format": "date", "description": "Start date (YYYY-MM-DD)."},
    "end_date": {"type": "string", "format": "date", "description": "End date (YYYY-MM-DD)."},
    "party_size": _integer("Number of people on the trip."),
    "conditions": _text("Expected conditions and notes."),
}


def _list_trips(identity, arguments):
    return facade.list_trips(identity)


def _get_trip(identity, arguments):
    return facade.get_trip(identity, arguments["trip_id"])


def _create_trip(identity, arguments):
    return facade.create_trip(identity, arguments)


def _list_gear(identity, arguments):
    return facade.list_gear(identity, arguments)


def _add_gear(identity, arguments):
    return facade.add_gear(identity, arguments)


def _update_gear(identity, arguments):
    return facade.update_gear(identity, arguments)


def _list_pantry(identity, arguments):
    return facade.list_pantry(identity)


def _add_pantry_item(identity, arguments):
    return facade.add_pantry_item(identity, arguments)


def _update_pantry_item(identity, arguments):
    return facade.update_pantry_item(identity, arguments)


def _list_verification_requests(identity, arguments):
    return facade.list_verification_requests(identity, arguments)


def _confirm_inventory_fact(identity, arguments):
    return facade.confirm_inventory_fact(identity, arguments)


def _plan_meals(identity, arguments):
    return facade.plan_meals(identity, arguments)


def _confirm_meal_plan(identity, arguments):
    return facade.confirm_meal_plan(identity, arguments)


def _review_intake(identity, arguments):
    return facade.review_intake(identity, arguments)


def _record_price(identity, arguments):
    return facade.record_price(identity, arguments)


TOOL_SPECS = [
    {
        "name": "campkit_list_trips",
        "description": "List the household's camping trips, newest first.",
        "inputSchema": _schema({}),
        "handler": _list_trips,
    },
    {
        "name": "campkit_get_trip",
        "description": "Read one trip, including its meals and estimated/confirmed packed weight.",
        "inputSchema": _schema({"trip_id": _uuid("Trip id.")}, ("trip_id",)),
        "handler": _get_trip,
    },
    {
        "name": "campkit_create_trip",
        "description": "Create a planned camping trip for the household.",
        "inputSchema": _schema(TRIP_PROPERTIES, ("name",)),
        "handler": _create_trip,
    },
    {
        "name": "campkit_list_gear",
        "description": "List shared gear with its estimated or confirmed weight and provenance.",
        "inputSchema": _schema({"category": _text("Only return gear in this category.")}),
        "handler": _list_gear,
    },
    {
        "name": "campkit_add_gear",
        "description": "Add a gear item as an estimated household record.",
        "inputSchema": _schema(GEAR_PROPERTIES, ("name",)),
        "handler": _add_gear,
    },
    {
        "name": "campkit_update_gear",
        "description": "Update fields on an existing household gear item.",
        "inputSchema": _schema({"gear_id": _uuid("Gear item id."), **GEAR_PROPERTIES}, ("gear_id",)),
        "handler": _update_gear,
    },
    {
        "name": "campkit_list_pantry",
        "description": "List pantry stock, keeping estimated and confirmed quantities distinct.",
        "inputSchema": _schema({}),
        "handler": _list_pantry,
    },
    {
        "name": "campkit_add_pantry_item",
        "description": "Add a pantry item as an estimated household record.",
        "inputSchema": _schema(PANTRY_PROPERTIES, ("name", "quantity", "unit")),
        "handler": _add_pantry_item,
    },
    {
        "name": "campkit_update_pantry_item",
        "description": "Update fields on an existing household pantry item.",
        "inputSchema": _schema(
            {"pantry_item_id": _uuid("Pantry item id."), **PANTRY_PROPERTIES},
            ("pantry_item_id",),
        ),
        "handler": _update_pantry_item,
    },
    {
        "name": "campkit_list_verification_requests",
        "description": "List verification requests for facts CampKit could not confirm.",
        "inputSchema": _schema(
            {"open_only": {"type": "boolean", "description": "Only open requests (default true)."}}
        ),
        "handler": _list_verification_requests,
    },
    {
        "name": "campkit_confirm_inventory_fact",
        "description": (
            "Confirm a gear or pantry fact as household-verified. Sensitive: requires confirm=true "
            "after the household member approves the prompt."
        ),
        "inputSchema": _schema(
            {
                "kind": {"type": "string", "enum": ["gear", "pantry"], "description": "Record kind."},
                "item_id": _uuid("Gear or pantry item id."),
                "confirm": CONFIRM_PROPERTY,
            },
            ("kind", "item_id"),
        ),
        "handler": _confirm_inventory_fact,
    },
    {
        "name": "campkit_plan_meals",
        "description": "Add meals (with pantry ingredients) to a planned trip.",
        "inputSchema": _schema(
            {
                "trip_id": _uuid("Trip id."),
                "meals": {
                    "type": "array",
                    "minItems": 1,
                    "description": "Meals to add.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": _text("Meal name."),
                            "servings": _integer("Servings this meal covers."),
                            "scheduled_for": {
                                "type": "string",
                                "format": "date-time",
                                "description": "When the meal is planned (ISO-8601).",
                            },
                            "notes": _text("Free-text notes."),
                            "ingredients": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "pantry_item_id": _uuid("Pantry item id."),
                                        "quantity": _number("Decimal quantity."),
                                        "unit": _text("Optional unit; defaults to the pantry unit."),
                                    },
                                    "required": ["pantry_item_id", "quantity"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
            },
            ("trip_id", "meals"),
        ),
        "handler": _plan_meals,
    },
    {
        "name": "campkit_confirm_meal_plan",
        "description": (
            "Reserve confirmed pantry stock for a trip and confirm it. Sensitive: requires "
            "confirm=true after the household member approves the prompt."
        ),
        "inputSchema": _schema(
            {"trip_id": _uuid("Trip id."), "confirm": CONFIRM_PROPERTY}, ("trip_id",)
        ),
        "handler": _confirm_meal_plan,
    },
    {
        "name": "campkit_review_intake",
        "description": (
            "Approve, correct, confirm, or reject a photo/barcode intake. Sensitive: requires "
            "confirm=true after the household member approves the prompt."
        ),
        "inputSchema": _schema(
            {
                "submission_id": _uuid("Intake submission id."),
                "action": {
                    "type": "string",
                    "enum": ["approve", "correct", "confirm", "reject"],
                    "description": "Review decision.",
                },
                "corrections": {
                    "type": "object",
                    "description": "Corrected candidate fields for action=correct.",
                },
                "note": _text("Optional review note."),
                "confirm": CONFIRM_PROPERTY,
            },
            ("submission_id", "action"),
        ),
        "handler": _review_intake,
    },
    {
        "name": "campkit_record_price",
        "description": "Record one dated, sourced price observation for a pantry item.",
        "inputSchema": _schema(
            {
                "pantry_item_id": _uuid("Pantry item id."),
                "amount": _number("Price amount, at most two decimal places."),
                "currency": {"type": "string", "description": "Three-letter currency code."},
                "merchant": _text("Merchant or retailer name."),
                "source": {
                    "type": "string",
                    "enum": ["manual", "receipt", "api", "feed"],
                    "description": "Where the observation came from.",
                },
                "source_url": _text("Citation link for the observation."),
                "observed_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": "When the price was observed (ISO-8601 with timezone).",
                },
                "confidence": _number("Confidence between 0 and 1."),
            },
            ("pantry_item_id", "amount"),
        ),
        "handler": _record_price,
    },
]

TOOLS_BY_NAME = {spec["name"]: spec for spec in TOOL_SPECS}


def list_tools() -> list[dict]:
    """Tool descriptors as advertised over MCP ``tools/list``."""
    return [
        {
            "name": spec["name"],
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
        }
        for spec in TOOL_SPECS
    ]


def _check_arguments(spec, arguments):
    if not isinstance(arguments, dict):
        raise facade.InvalidRequest("Tool arguments must be a JSON object.")
    properties = spec["inputSchema"]["properties"]
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise facade.InvalidRequest(f"Unsupported arguments: {', '.join(unknown)}.")
    for name in spec["inputSchema"]["required"]:
        if arguments.get(name) in (None, ""):
            raise facade.InvalidRequest(f"Missing required argument: {name}.")


def call_tool(name, arguments, identity):
    """Run one allowlisted tool.

    Returns ``(payload, is_error)``. A confirmation-gated tool that was called
    without ``confirm=true`` returns a ``confirmation_required`` payload with
    ``is_error`` false: the call succeeded, the mutation simply waits for a
    human decision. Genuine failures set ``is_error`` true.
    """
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return {"status": "error", "error": {"code": "unknown_tool", "message": "Unknown tool."}}, True
    try:
        _check_arguments(spec, arguments or {})
        result = spec["handler"](identity, arguments or {})
        return {"status": "ok", **result}, False
    except facade.ConfirmationRequired as exc:
        return {"status": "confirmation_required", "confirmation": exc.as_dict()}, False
    except facade.NotFound:
        return _failure("not_found", "The requested record was not found.")
    except (ObjectDoesNotExist, PermissionDenied):
        return _failure("not_found", "The requested record was not found.")
    except facade.InvalidRequest as exc:
        return _failure("invalid_arguments", str(exc))
    except ValidationError as exc:
        message, fields = facade.format_validation_error(exc)
        return _failure("validation_error", message or "The payload is invalid.", fields=fields)


def _failure(code, message, **extra):
    return {"status": "error", "error": {"code": code, "message": message, **extra}}, True
