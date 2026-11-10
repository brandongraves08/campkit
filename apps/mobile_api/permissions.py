from django.http import JsonResponse


def unauthorized():
    return JsonResponse(
        {"error": {"code": "unauthorized", "message": "Authentication required."}},
        status=401,
    )


def not_found():
    return JsonResponse(
        {"error": {"code": "not_found", "message": "Not found."}},
        status=404,
    )


def authenticate_mobile(view):
    """Authenticate each route using the mobile packing OAuth scope."""
    from functools import wraps

    from apps.mobile_api.auth import authenticate_mobile_request

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if authenticate_mobile_request(request) is None:
            return unauthorized()
        return view(request, *args, **kwargs)

    return wrapped
