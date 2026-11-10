from django.http import JsonResponse
from django.urls import path
from oauth2_provider import views as provider_views


class MobileAuthorizationView(provider_views.AuthorizationView):
    """Require authorization code + S256 PKCE before entering browser login."""

    def dispatch(self, request, *args, **kwargs):
        if request.method == "GET":
            if request.GET.get("response_type") != "code":
                return JsonResponse({"error": "unsupported_response_type"}, status=400)
            if not request.GET.get("code_challenge") or request.GET.get("code_challenge_method") != "S256":
                return JsonResponse({"error": "invalid_request"}, status=400)
        return super().dispatch(request, *args, **kwargs)


class MobileTokenView(provider_views.TokenView):
    """Enforce the mobile server's runtime grant allowlist before DOT dispatch."""

    def post(self, request, *args, **kwargs):
        if request.POST.get("grant_type") not in {"authorization_code", "refresh_token"}:
            return JsonResponse({"error": "unsupported_grant_type"}, status=400)
        return super().post(request, *args, **kwargs)


app_name = "oauth2_provider"
urlpatterns = [
    path("authorize/", MobileAuthorizationView.as_view(), name="authorize"),
    path("token/", MobileTokenView.as_view(), name="token"),
    path("revoke_token/", provider_views.RevokeTokenView.as_view(), name="revoke-token"),
]
