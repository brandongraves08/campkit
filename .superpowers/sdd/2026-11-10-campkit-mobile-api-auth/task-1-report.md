# Task 1 report: OAuth provider and security boundary

## Gate result

**PASS — provider selected and pinned.** `django-oauth-toolkit==3.4.1` is the latest non-prerelease, non-yanked release shown by the PyPI project metadata and the latest release shown by the project's GitHub releases API at verification time. The current official docs identify themselves as version 3.4.1. The project is marked Production/Stable on PyPI. The selected release supports Django 5.2; the isolated resolver confirms exact compatibility with CampKit's `Django==5.2.18`, without a Django/framework upgrade. No blocking incompatibility was found.

The dependency is now pinned in `pyproject.toml`. This is a provider verification/pinning task, not the provider integration: no URLs, settings, migrations, OAuth application records, endpoint implementations, or user-facing behavior were added or changed.

## Official sources and exact versions observed

- [Official Django OAuth Toolkit documentation, latest](https://django-oauth-toolkit.readthedocs.io/en/latest/) — fetched successfully; document title/version `Django OAuth Toolkit 3.4.1 documentation`; source content describes toolkit 3.4.0+ MCP authorization and declares Django 5.2 supported.
- [Official DOT settings reference](https://django-oauth-toolkit.readthedocs.io/en/latest/settings.html) — `PKCE_REQUIRED=True`; scopes configurable through `SCOPES`/`DEFAULT_SCOPES` or scopes backend; `ROTATE_REFRESH_TOKEN=True`; `ALLOW_URI_WILDCARDS=False`; `OAUTH2_GRANT_TYPES_SUPPORTED` documents a broad default list including `password` and `implicit`.
- [Official DOT security guidance](https://django-oauth-toolkit.readthedocs.io/en/latest/security.html) — exact redirect matching with wildcards off; refresh rotation and optional replay/family protection; recommends enabling the RFC 9700 settings. It also documents that the refresh replay protection default is `False`, which must be turned on by CampKit.
- [Official DOT advanced topics](https://django-oauth-toolkit.readthedocs.io/en/latest/advanced_topics.html) — default/custom scope backend, URI validators, redirect URI registration/request-time matching constraints.
- [Official DOT 3.4.1 GitHub release](https://github.com/django-oauth/django-oauth-toolkit/releases/tag/3.4.1) — release published `2026-08-21`; release notes cover exact redirect matching and refresh token rotation/revocation hardening.
- [Official DOT source at tag 3.4.1: `oauth2_provider/views/base.py`](https://github.com/django-oauth/django-oauth-toolkit/blob/3.4.1/oauth2_provider/views/base.py) — `BaseAuthorizationView` inherits Django `LoginRequiredMixin`; login redirects use Django's configured login URL.
- [PyPI JSON project metadata](https://pypi.org/pypi/django-oauth-toolkit/json) — observed current version `3.4.1`, release URL `https://pypi.org/project/django-oauth-toolkit/3.4.1/`, not yanked, Python `>=3.10`, Django `>=4.2`, and classifiers include Django 5.2. Toolkit metadata's lower bound is general; the official release docs/classifier provide the specific 5.2 support confirmation.
- Installed project pin: `Django==5.2.18` (`pyproject.toml`). Resolver candidate: `django-oauth-toolkit==3.4.1`.

## Required capability findings

| Requirement | Evidence and result |
|---|---|
| Current maintained release | PASS: official docs and GitHub latest release showed 3.4.1; PyPI showed the same version, non-yanked, Production/Stable. |
| Django 5.2.18 | PASS: official docs list Django 5.2; PyPI metadata requires Django >=4.2 and classifies Django 5.2; exact pins resolved together. |
| Public client PKCE | PASS: official settings reference documents `PKCE_REQUIRED=True` by default, and says public clients MUST use PKCE. Follow-on should retain PKCE and favor S256 (RFC 9700 compliance setting). |
| Refresh rotation/revocation | PASS: rotation defaults on; provider supports refresh family replay protection and token revocation. 3.4.1 release notes document that revoking an access token also revokes its paired refresh token. Replay protection is default-off and needs explicit `True` setting. |
| Scopes | PASS: settings-driven `SCOPES`, `DEFAULT_SCOPES`, and replaceable scope backend are documented; scope validation rejects requested scopes not offered by the backend. CampKit must configure least-privilege scopes and enforce authorization in API views. |
| Strict redirect URI restrictions | PASS: exact URI matching; wildcards default off. Keep `ALLOW_URI_WILDCARDS=False`; register only exact approved callback URIs. |
| Existing browser login compatibility | PASS at provider-level: authorization view uses Django `LoginRequiredMixin` and Django `LOGIN_URL`; CampKit has existing session/authentication middleware, `LOGIN_URL = "login"`, and the `login` named route points at `/accounts/login/`. This reuses the existing member's browser login session; no password grant is needed. Runtime end-to-end behavior is not claimed/tested in this provider-only task. |
| No password grant | Provider supports an explicit `OAUTH2_GRANT_TYPES_SUPPORTED` allowlist. Its documented defaults include password and implicit, so follow-on integration must set an allowlist of only required grants (authorization code and refresh token), rather than relying on defaults. |

## Security boundary / follow-on decisions

- OAuth access tokens identify Django member users. API access must additionally require an active `Membership` for the household being accessed; OAuth provider authentication does not establish household authorization.
- Never use the Hermes service credential as an end-user OAuth member identity.
- Limit enabled grants explicitly to `authorization_code` and `refresh_token`; exclude `password`, `implicit`, and `client_credentials`.
- Use public client registrations and PKCE; no client secret for a native public client.
- Keep wildcard redirect support disabled and use exact, explicitly registered redirect URIs.
- Use small explicit scopes; enable refresh replay protection; choose a finite refresh lifetime. If hashed token storage is enabled later, note documented incompatibility with nonzero refresh-token grace periods (leave grace at zero).
- Mount OAuth endpoints without disturbing existing `/api/v1/`, web, or MCP behavior.

## Commands and real results

1. `curl -fsSL --max-time 30 https://pypi.org/pypi/django-oauth-toolkit/json` — exit 0; metadata showed `version=3.4.1`, `yanked=false`, `requires_python=>=3.10`, `requires_dist` includes `django>=4.2`, and Django 5.2 classifier. Output exceeded terminal display, full result was saved by terminal at `/root/.hermes/profiles/implementer/cache/terminal-output/out-1794329223-1824198-f950.log`.
2. `curl -fsSL --max-time 30 https://django-oauth-toolkit.readthedocs.io/en/latest/` — exit 0; rendered official docs title/version `3.4.1`; Django 5.2 requirements present.
3. `curl -fsSL --max-time 30 https://api.github.com/repos/django-oauth/django-oauth-toolkit/releases/latest` — exit 0; release tag `3.4.1`, `published_at=2026-08-21T03:45:48Z`; release notes report redirect URI exact-match and refresh-token security changes.
4. `curl -fsSL --max-time 30 https://django-oauth-toolkit.readthedocs.io/en/latest/settings.html | grep -E -i -A 12 -B 3 'id="oauth2-grant-types-supported"|id="allow-uri-wildcards"|id="rotate-refresh-token"|id="refresh-token-reuse-protection"'` — exit 0; output confirmed wildcard default False, rotation default True, replay protection default False and broad grant defaults including password/implicit.
5. `curl -fsSL --max-time 30 https://django-oauth-toolkit.readthedocs.io/en/latest/security.html | grep -E -i -C 2 'PKCE|refresh|redirect|wildcard|public|client'` — exit 0; output documented PKCE, exact redirects, replay handling, and compliance settings.
6. `curl -fsSL --max-time 30 https://raw.githubusercontent.com/django-oauth/django-oauth-toolkit/3.4.1/oauth2_provider/views/base.py | grep -E -i -C 2 'LoginRequiredMixin|login_url|login_required'` — exit 0; source shows `BaseAuthorizationView(LoginRequiredMixin, ...)`.
7. Isolated candidate-only check: `uv pip compile --python-version 3.14 --no-emit-package campkit -o /root/.hermes/profiles/implementer/cache/scratch/campkit-oauth-resolution.txt /root/.hermes/profiles/implementer/cache/scratch/campkit-oauth-resolution.in` — exit 0; `Resolved 15 packages in 221ms`, including `django==5.2.18` and `django-oauth-toolkit==3.4.1`.
8. Isolated project-manifest resolution: `uv pip compile pyproject.toml --no-emit-package campkit -o /root/.hermes/profiles/implementer/cache/scratch/campkit-project-resolved.txt` — exit 0; `Resolved 17 packages in 101ms`, with exact CampKit Django and psycopg pins and `django-oauth-toolkit==3.4.1`; no framework upgrade.
9. `python -m pytest -q` — exit 1 because the ambient interpreter has no pytest (`No module named pytest`).
10. Isolated full suite: `uv run --no-project --with 'Django==5.2.18' --with 'django-oauth-toolkit==3.4.1' --with 'psycopg[binary]==3.3.6' --with 'pytest==9.0.3' --with 'pytest-django==4.14.0' pytest -q` — exit 0; `369 passed in 125.12s (0:02:05)`.

No provider behavioral test was added: this task verifies/pins the dependency and documents the contract; no OAuth runtime integration exists in this scoped change. The full existing suite was run in an isolated environment with the selected dependency present.

## Files changed

- `pyproject.toml` — added exact runtime pin `django-oauth-toolkit==3.4.1`.
- `docs/mobile-api-auth-implementation-notes.md` — provider decision and follow-on security boundary.
- `.superpowers/sdd/2026-11-10-campkit-mobile-api-auth/task-1-report.md` — evidence, commands, decisions, tests, and concerns.

Unrelated pre-existing untracked files (`docs/superpowers/plans/2026-11-10-campkit-mobile-api-auth.md`, `task-artifacts-pantrypolish/**`, and `uv.lock`) were left untouched and will not be staged.
