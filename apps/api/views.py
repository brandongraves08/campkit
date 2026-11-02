"""Authenticated, additive ``/api/v1`` endpoints.

Authentication is bearer-token only: the session middleware never authorizes a
request here, so there is no cookie-authenticated path to protect with CSRF.
Every response is JSON, and every lookup is pinned to the token's household so a
foreign id and a nonexistent id produce the same ``404``.
"""

import functools
import json

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.api import facade
from apps.api.auth import TokenRejected, authenticate_token, bearer_value


def _error(status, code, message, **extra):
    body = {"error": {"code": code, "message": message}}
    body["error"].update(extra)
    response = JsonResponse(body, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _confirmation_response(exc):
    return _error(
        409,
        "confirmation_required",
        exc.prompt,
        action=exc.action,
        confirmation=exc.as_dict(),
    )


def _json_body(request):
    if request.method not in {"POST", "PATCH"}:
        return {}
    content_type = request.content_type or ""
    if not content_type.startswith("application/json"):
        raise facade.InvalidRequest("Requests must use application/json.")
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise facade.InvalidRequest("Request body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise facade.InvalidRequest("Request body must be a JSON object.")
    return payload


def api_endpoint(*, methods):
    """Authenticate, parse, dispatch, and map errors to one stable JSON shape."""

    def decorator(view):
        @csrf_exempt
        @functools.wraps(view)
        def wrapped(request, **kwargs):
            if request.method not in methods:
                response = _error(
                    405, "method_not_allowed", "This operation is not available at this endpoint."
                )
                response["Allow"] = ", ".join(methods)
                return response
            try:
                raw_token = bearer_value(request.headers.get("Authorization", ""))
                identity = authenticate_token(raw_token)
            except TokenRejected:
                return _error(401, "unauthorized", "A valid API token is required.")
            try:
                payload = _json_body(request)
                body = view(request, identity, payload, **kwargs)
            except facade.NotFound:
                return _error(404, "not_found", "Not found.")
            except (ObjectDoesNotExist, PermissionDenied):
                # A foreign-household primary key and a missing one stay
                # indistinguishable, and a scope failure never leaks existence.
                return _error(404, "not_found", "Not found.")
            except facade.ConfirmationRequired as exc:
                return _confirmation_response(exc)
            except facade.InvalidRequest as exc:
                return _error(400, "invalid_request", str(exc))
            except ValidationError as exc:
                message, fields = facade.format_validation_error(exc)
                return _error(400, "validation_error", message or "The payload is invalid.", fields=fields)
            response = JsonResponse(body, status=200, safe=False)
            response["Cache-Control"] = "no-store"
            return response

        return wrapped

    return decorator


# --------------------------------------------------------------------------- #
# Trips
# --------------------------------------------------------------------------- #


@api_endpoint(methods=("GET", "POST"))
def trip_collection(request, identity, payload):
    if request.method == "POST":
        return facade.create_trip(identity, payload)
    return facade.list_trips(identity)


@api_endpoint(methods=("GET",))
def trip_detail(request, identity, payload, trip_id):
    return facade.get_trip(identity, trip_id)


@api_endpoint(methods=("POST",))
def trip_meal_plan(request, identity, payload, trip_id):
    return facade.plan_meals(identity, {**payload, "trip_id": trip_id})


@api_endpoint(methods=("POST",))
def trip_meal_confirm(request, identity, payload, trip_id):
    return facade.confirm_meal_plan(identity, {**payload, "trip_id": trip_id})


# --------------------------------------------------------------------------- #
# Gear
# --------------------------------------------------------------------------- #


@api_endpoint(methods=("GET", "POST"))
def gear_collection(request, identity, payload):
    if request.method == "POST":
        return facade.add_gear(identity, payload)
    return facade.list_gear(identity, payload)


@api_endpoint(methods=("GET", "PATCH"))
def gear_detail(request, identity, payload, item_id):
    if request.method == "PATCH":
        return facade.update_gear(identity, {**payload, "gear_id": item_id})
    return facade.get_gear(identity, item_id)


@api_endpoint(methods=("POST",))
def gear_confirm(request, identity, payload, item_id):
    return facade.confirm_inventory_fact(identity, {**payload, "kind": "gear", "item_id": item_id})


# --------------------------------------------------------------------------- #
# Pantry
# --------------------------------------------------------------------------- #


@api_endpoint(methods=("GET", "POST"))
def pantry_collection(request, identity, payload):
    if request.method == "POST":
        return facade.add_pantry_item(identity, payload)
    return facade.list_pantry(identity)


@api_endpoint(methods=("GET", "PATCH"))
def pantry_detail(request, identity, payload, item_id):
    if request.method == "PATCH":
        return facade.update_pantry_item(identity, {**payload, "pantry_item_id": item_id})
    return facade.get_pantry(identity, item_id)


@api_endpoint(methods=("POST",))
def pantry_confirm(request, identity, payload, item_id):
    return facade.confirm_inventory_fact(identity, {**payload, "kind": "pantry", "item_id": item_id})


@api_endpoint(methods=("GET", "POST"))
def pantry_prices(request, identity, payload, item_id):
    if request.method == "POST":
        return facade.record_price(identity, {**payload, "pantry_item_id": item_id})
    return facade.price_history(identity, item_id)


@api_endpoint(methods=("GET",))
def pantry_nutrition(request, identity, payload, item_id):
    return facade.nutrition_facts(identity, item_id)


# --------------------------------------------------------------------------- #
# Intake and verification
# --------------------------------------------------------------------------- #


@api_endpoint(methods=("GET",))
def intake_collection(request, identity, payload):
    return facade.list_intake(identity)


@api_endpoint(methods=("GET",))
def intake_detail(request, identity, payload, submission_id):
    return facade.get_intake(identity, submission_id)


@api_endpoint(methods=("POST",))
def intake_review(request, identity, payload, submission_id):
    return facade.review_intake(identity, {**payload, "submission_id": submission_id})


@api_endpoint(methods=("GET",))
def verification_request_collection(request, identity, payload):
    open_only = request.GET.get("open_only")
    options = {}
    if open_only is not None:
        options["open_only"] = open_only.lower() != "false"
    return facade.list_verification_requests(identity, options)
