# Mobile API OAuth implementation notes — Task 1

## Provider decision

**Gate passed:** use `django-oauth-toolkit==3.4.1` as CampKit's OAuth 2.0 provider with the installed `Django==5.2.18`. The package metadata requires `Django>=4.2`; official maintained documentation lists Django 5.2; an isolated exact-pin resolver run succeeded without changing the checkout. See `task-1-report.md` for source URLs and exact resolver output.

The provider supports authorization-code flow, public clients with PKCE, refresh-token rotation and family revocation on replay, configurable scopes, registered redirect URI validation and exact request-time redirect matching. Its authorization view is protected by Django `LoginRequiredMixin`, honoring `LOGIN_URL`; CampKit currently has `LOGIN_URL = "login"`, with the named route at `/accounts/login/`, and Django authentication/session middleware already enabled. This supports reusing the existing browser login session; it does not require a password grant.

## Security and integration boundary for follow-on work

- Explicitly configure supported grants to the minimum required: `authorization_code` and `refresh_token`; do not expose `password`, `implicit`, or `client_credentials` grants. The toolkit's documented default grant list includes password and implicit, so the secure allowlist must be set rather than relying on defaults.
- Use public native-client registration (`client_type="public"`), authorization-code flow, and PKCE. Toolkit `PKCE_REQUIRED` defaults to `True`; retain it. Prefer S256 and set the corresponding RFC 9700 compliance setting when configuring the provider.
- Keep redirect URI wildcards disabled (`ALLOW_URI_WILDCARDS=False`, toolkit default). Register exact native callback URIs; request-time matching is exact. Do not enable wildcard redirects.
- Configure minimal explicit scopes and a least-privilege default scope list. Requested scopes are checked against the configured/selected scopes backend; endpoint authorization still belongs to CampKit.
- Keep refresh rotation enabled (`ROTATE_REFRESH_TOKEN=True`, default), enable reuse protection to revoke a replayed token family, provide a finite refresh lifetime, and preserve the zero grace-period default if opting into hashed token storage. The provider exposes the RFC 7009 revocation endpoint; revoking access tokens also invalidates their bound refresh token in 3.4.1.
- Authorize API requests as the OAuth token's Django member identity and require that member's active `Membership` for household data. Never treat the Hermes service credential as an end-user OAuth identity or use it to authorize member requests.
- Mount OAuth endpoints separately from the existing `/api/v1/` routes; do not alter web or MCP behavior. This Task 1 only verifies and pins the provider; it does not add endpoints, settings, migrations, or deployment changes.

## Compatibility caveats

The default grant allowlist contains more grants than CampKit needs, and refresh reuse protection is documented as default-off. Follow-on provider configuration must explicitly turn off unneeded grants and enable replay protection. Version 3.4.1 tightens redirect URI matching and refresh-token revocation behavior; this task makes no database migration or runtime behavior change, so the full live browser/API compatibility check remains part of the subsequent implementation and QA.
