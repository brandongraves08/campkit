"""Resolve mobile OAuth bearer tokens to their Django member identity.

Household membership is a separate authorization decision owned by API views;
this helper never authenticates the shared Hermes service identity.
"""

from oauth2_provider.oauth2_backends import get_oauthlib_core


def authenticate_mobile_request(request):
    """Attach the authenticated OAuth token owner as ``request.user``.

    Return the access-token object on success and ``None`` for missing, invalid,
    revoked, or expired credentials. Callers must still check active Membership.
    """
    valid, oauth_request = get_oauthlib_core().verify_request(request, scopes=[])
    if not valid:
        return None
    request.user = oauth_request.user
    return oauth_request.access_token
