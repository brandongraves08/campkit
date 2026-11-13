# Mobile API OAuth implementation notes — Task 1

## Provider decision

**Gate passed:** use `django-oauth-toolkit==3.4.1` as CampKit's OAuth 2.0 provider with the installed `Django==5.2.18`. The package metadata requires `Django>=4.2`; official maintained documentation lists Django 5.2; an isolated exact-pin resolver run succeeded without changing the checkout. See `task-1-report.md` for source URLs and exact resolver output.

The provider supports authorization-code flow, public clients with PKCE, refresh-token rotation and family revocation on replay, configurable scopes, registered redirect URI validation and exact request-time redirect matching. Its authorization view is protected by Django `LoginRequiredMixin`, honoring `LOGIN_URL`; CampKit currently has `LOGIN_URL = "login"`, with the named route at `/accounts/login/`, and Django authentication/session middleware already enabled. This supports reusing the existing browser login session; it does not require a password grant.

## Security and integration boundary for follow-on work

- Explicitly enforce the secure flows, not just server metadata: set the RFC 9700 password/implicit/PKCE/redirect/refresh-token compliance gates to `True`, retain `PKCE_REQUIRED=True`, `ALLOW_URI_WILDCARDS=False`, and `ROTATE_REFRESH_TOKEN=True`, enable `REFRESH_TOKEN_REUSE_PROTECTION=True`, use a zero grace period, 15-minute access tokens, and 30-day idle refresh-token expiry. `OAUTH2_GRANT_TYPES_SUPPORTED` is discovery metadata only; it does not reject grants at request time. Set that metadata to advertise only Authorization Code and refresh token, and provision only the intended public authorization-code application. Do not expose dynamic client registration or other application records/grants.
- Use public native-client registration (`client_type="public"`), authorization-code flow, and PKCE. Do not include a client secret in the app. Use S256 only; the plain method must be rejected.
- Keep redirect URI wildcards disabled (`ALLOW_URI_WILDCARDS=False`); register exactly `com.graveystudios.campkit://oauth/callback`, and require exact request-time matching. Do not enable wildcard redirects.
- Use the explicit least-privilege scope `mobile:packing`; endpoint authorization still belongs to CampKit.
- Keep refresh rotation and reuse protection enabled so replay revokes the token family; retain zero grace. The provider exposes the RFC 7009 revocation endpoint.
- Authorize API requests as the OAuth token's Django member identity and require that member's active `Membership` for household data. Never treat the Hermes service credential as an end-user OAuth identity or use it to authorize member requests.
- Mount OAuth endpoints separately from the existing `/api/v1/` routes; do not alter web or MCP behavior. This Task 1 only verifies and pins the provider; it does not add endpoints, settings, migrations, or deployment changes.

## Compatibility caveats

The documented grant-types metadata list advertises more flows than CampKit needs, but it is not a runtime enforcement allowlist. Follow-on provider configuration must explicitly enable the RFC 9700 password/implicit gates, provision only the intended public authorization-code application, and enable refresh-token replay protection (which defaults off). Version 3.4.1 tightens redirect URI matching and refresh-token revocation behavior; this task makes no database migration or runtime behavior change, so the full live browser/API compatibility check remains part of the subsequent implementation and QA.
