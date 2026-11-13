"""Resolve mobile OAuth bearer tokens to their Django member identity.

Household membership is a separate authorization decision owned by API views;
this helper never authenticates the shared Hermes service identity.
"""

from oauth2_provider.oauth2_backends import get_oauthlib_core


def authenticate_mobile_request(request):
    """Attach the authenticated OAuth token owner as ``request.user``.

    Return the access-token object on success and ``None`` for missing, invalid,
    revoked, expired, user-less, or deactivated-user credentials. Callers must still check active Membership.
    """
    valid, oauth_request = get_oauthlib_core().verify_request(request, scopes=["mobile:packing"])
    if not valid or oauth_request.user is None or not oauth_request.user.is_active:
        return None
    request.user = oauth_request.user
    return oauth_request.access_token
