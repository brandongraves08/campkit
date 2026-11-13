# Task 2 report: member OAuth authorization-code + PKCE

## Summary

Implemented the dedicated `/o/` OAuth routes for a public native member client. The configured callback is exactly `com.graveystudios.campkit://oauth/callback`, scope vocabulary is only `mobile:packing`, access tokens last 15 minutes, and refresh tokens expire after 30 days of idle use with rotation, replay protection, and zero grace. OAuth authentication resolves a bearer token to its Django user; it does not authorize household access, which remains a separate active-`Membership` requirement for the future API views.

## Files changed

- `config/settings.py` — installed the OAuth provider and mobile app; explicit scope, lifetime, redirect scheme, exact matching, PKCE/S256 and RFC 9700 settings, refresh rotation/reuse protection, metadata restricted to code and refresh grants.
- `config/urls.py` — mounted the separate OAuth URLs under `/o/`, leaving `/api/v1/` unchanged.
- `apps/mobile_api/__init__.py`, `apps/mobile_api/apps.py` — app package/config.
- `apps/mobile_api/oauth_urls.py` — provider authorization/token/revocation routes only. Authorization rejects missing/non-S256 PKCE and non-code response types. Token view rejects runtime grants other than authorization_code and refresh_token (the provider grant setting itself is discovery metadata only). No device, introspection, DCR, management, or OIDC routes are mounted.
- `apps/mobile_api/auth.py` — helper uses provider validation to resolve valid OAuth bearer tokens to `request.user`; invalid credentials return `None`.
- `tests/mobile_api/test_oauth_pkce.py`, `tests/mobile_api/test_token_revocation.py` — behavioral regression assertions for settings and authorization boundaries, member identity resolution, unknown/expired/revoked bearer rejection, real `mobile:packing` authorization-code/token issuance, unrelated-scope rejection, refresh rotation/replay family revocation, and token revocation.
- `.superpowers/sdd/2026-11-10-campkit-mobile-api-auth/task-1-report.md` — corrected the statement that grant discovery metadata is runtime enforcement.

## Decisions / constraints

- Preserved the existing `/api/v1/` route and member web login URL; did not modify MCP behavior.
- Configured only public native-client behavior; no app client secret is required or exposed.
- Set `OAUTH2_GRANT_TYPES_SUPPORTED` to `authorization_code` and `refresh_token` for metadata, but separately enforce runtime grant types in the token view. Password and implicit gates are explicitly enabled; client-credentials requests are denied by the mobile token view. PKCE requires S256, exact redirects, and the required RFC 9700 compliance gates are enabled.
- The future API must enforce active `Membership` on every data endpoint. OAuth authentication alone is not household authorization and this task does not prove Task 3 endpoint behavior.
- OAuth application row provisioning and deployment were not performed; no backend exposure, push, publication, migration, or external action occurred.

## Tests and actual results

1. Initial test-first focused red run: `.venv/bin/python -m pytest tests/mobile_api/test_oauth_pkce.py tests/mobile_api/test_token_revocation.py -q` — **2 failed, 18 passed in 14.97s**: it exposed missing authorization-scope rejection; the replay failure showed the synthetic refresh fixture had no `token_family`. After adding an assertion that the rotated access token is invalidated, `.venv/bin/python -m pytest tests/mobile_api/test_token_revocation.py::test_refresh_token_rotates_and_invalidates_the_previous_token -q` returned **1 failed in 4.21s** because that same fixture omitted the real provider family identifier. The production scope guard was added and the fixture was corrected to use a UUID family, matching provider-issued refresh chains.
2. Review follow-up: added an assertion that, after replay denial, the persisted rotated `payload["refresh_token"]` record itself has a non-null `revoked` timestamp. `.venv/bin/python -m pytest tests/mobile_api/test_token_revocation.py::test_refresh_token_rotates_and_invalidates_the_previous_token -q` — **1 passed in 3.94s** (successful first refresh control, then replay denial and family revocation confirmed).
3. Final focused verification: `.venv/bin/python -m pytest tests/mobile_api/test_oauth_pkce.py tests/mobile_api/test_token_revocation.py -q` — **20 passed in 14.57s**.
4. Full suite: `.venv/bin/python -m pytest -q` — **389 passed in 142.54s**.

## Acceptance rules → tests → result

- Exact callback, public authorization-code client, mobile scope, lifetimes, explicit metadata and security gates → `test_provider_settings_are_minimal_and_secure`, `test_public_client_is_authorization_code_only_and_has_exact_callback` → passed.
- Existing browser login used → `test_authorization_uses_existing_member_browser_login` → passed.
- Only code + S256 PKCE; invalid callback, absent/plain challenge and implicit response denied → `test_authorization_rejects_unregistered_redirect_uri`, `test_authorization_rejects_missing_or_plain_pkce`, `test_authorize_endpoint_rejects_implicit_response` → passed.
- Runtime password/client-credentials rejection → `test_token_endpoint_rejects_unapproved_grant_types` → passed.
- Valid OAuth bearer resolves to the Django member; unknown, expired, and revoked tokens return no principal without writes → `test_bearer_authentication_resolves_member_and_denies_invalid_credentials_without_writes` → passed (valid + 3 denial states).
- Access expiry and revocation → `test_access_token_expiry_is_finite_and_oauth_provider_rejects_expired_token`, `test_revocation_endpoint_invalidates_access_token` → passed.
- Refresh rotation, replay denial, and revocation of the rotated refresh token and its associated access-token family → `test_refresh_token_rotates_and_invalidates_the_previous_token` → passed (successful refresh control; replay returns 400; rotated refresh record has a revocation timestamp; access token is invalidated).
- Actual `mobile:packing` token issuance and unrelated-scope refusal → `test_authorization_issues_mobile_scope_and_rejects_unregistered_scope` → passed.

## Remaining limitations

- The registered OAuth Application row is not provisioned as a migration or during this code-only task; an authorized operator will need to create the single public app record with the exact callback before use. No client id has been issued for the mobile app.
- No Task 3 data endpoint exists yet, so common `401` response envelopes and proof of no data write for unknown/revoked/expired credentials are outside this task's executable interface. The auth helper returns no user/token for invalid credentials; views must translate that into the agreed 401 and must still enforce active Membership.
- No deployment, runtime production configuration, or live browser/mobile end-to-end run was performed.
