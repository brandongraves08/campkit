# CampKit mobile API contract (v1)

This contract describes the mobile endpoints implemented under `/api/mobile/v1/` and the OAuth provider configuration in this repository. It is for a native client; it does not change or replace the existing `/api/v1/`, web, or MCP interfaces.

## Network and base URL

The API is not a public service. The client must connect to the self-hosted CampKit host over the household's trusted LAN or VPN, using the HTTPS origin established by its TLS-terminating reverse proxy. Set the client base URL to that origin and append `/api/mobile/v1/` (for example, `https://campkit.lan/api/mobile/v1/`). Do not send bearer tokens over plaintext HTTP. The web container itself binds to loopback; the proxy must forward HTTPS using the deployment's `X-Forwarded-Proto` configuration. Do not expose this service publicly. This backend work does not include live production deployment or provisioning an OAuth Application record.

## Authorization: native authorization code + PKCE

The provider is configured for the OAuth authorization-code and refresh-token grants; it requires PKCE with `S256`, exact registered redirect-URI matching, and no wildcard redirect URIs. Implicit, password, and client-credentials grants are not supported. The only configured scope is `mobile:packing`, which is also the default scope. Access tokens expire after 900 seconds; refresh tokens expire after 30 days and rotate with reuse protection and a zero-second grace period. Dynamic client registration is disabled.

The registered native callback used by the contract and tests is exactly:

`com.graveystudios.campkit://oauth/callback`

Use the system browser for `/o/authorize/` and `/o/token/` (the OAuth endpoints are mounted under `/o/`). Send `response_type=code`, the registered `client_id`, exact `redirect_uri`, `scope=mobile:packing`, a high-entropy PKCE `code_verifier`, its Base64URL-without-padding SHA-256 `code_challenge`, and `code_challenge_method=S256`. The member authorizes using CampKit's existing browser login/consent flow. Exchange the returned authorization code at `/o/token/` with `grant_type=authorization_code`, `code`, the same `redirect_uri`, `client_id`, and `code_verifier`. Refresh using the provider's `refresh_token` grant and rotate/store the returned refresh token. The client is public and has no client secret.

Attach the access token to every mobile API request as `Authorization: Bearer <access-token>`. Missing, unknown, expired, revoked, malformed, underscoped, user-less, or inactive-user credentials return the same 401 response. A valid token must identify an active member user and carry `mobile:packing`.

## Household and trip selection

There is no mobile “current household” selector in this API. `GET /households/` returns all households for which the token's member currently has a membership; an empty list is valid. Select one by using its ID in subsequent household paths. Each route checks membership against that requested household, independently of any household selected in the web UI. A missing, malformed, foreign, or no-longer-authorized household/trip/packing row is returned as the same 404 envelope, so clients must not distinguish those cases. Membership is rechecked on each request; cached household/trip data does not retain authorization.

## Read endpoints

All endpoints below require a valid bearer token and accept GET only.

### `GET /households/`

Response: `{"households":[{"id":"<uuid>","name":"<string>"}]}`. The list is ordered by name, then ID.

### `GET /households/{household_id}/trips/`

`household_id` and trip IDs are UUIDs. Response is `{"trips":[...]}`. Each trip contains exactly:

- `id`: UUID string
- `name`: string
- `destination`: string
- `start_date`, `end_date`: ISO date string or `null`
- `party_size`: integer
- `conditions`: string
- `status`: trip status string

### `GET /households/{household_id}/trips/{trip_id}/packing/`

Response has exactly `trip` and `packing` top-level keys. `trip` contains `id` (UUID string) and `name`. Each packing row contains `id` (integer), `packed` (boolean), `quantity` (integer), `version` (positive integer), and `gear` with `id` (UUID string), `name`, and `category` strings. Rows are ordered by row ID. Empty trips return `"packing": []`. These snapshots intentionally do not include owner, pantry, meal, or provenance data.

## Packing mutation and concurrency

### `PUT /households/{household_id}/trips/{trip_id}/packing/{entry_id}/`

The request body must be a JSON object with exactly these keys and types:

```json
{
  "packed": true,
  "client_operation_id": "<UUID>",
  "base_version": 1
}
```

`packed` must be a JSON boolean (not a string or integer), `client_operation_id` must be a UUID string, and `base_version` must be an integer greater than zero (a boolean is not an integer here). Extra or missing keys, invalid JSON, or invalid values return `400 invalid_request` without changing packing state.

On success, the response is `200` with `entry_id` (string), `packed` (boolean), `version` (integer), and `client_operation_id` (UUID string). New packing rows start at version 1. A write whose `base_version` matches the current version may change the desired `packed` state; a real state change increments that row's version by one. Repeating the same desired state is a no-op and does not increment the version. Versions are per row. If the base version is stale, the API returns `409 version_conflict` including the current `packed` and `version`; it does not change state. Completed or cancelled trips are readable but read-only, and a write returns `409 trip_read_only` without mutation.

### Idempotency and retries

Generate a UUID `client_operation_id` per logical operation and persist it across retries. Within a member's account, the operation UUID is unique across packing writes. Repeating the same UUID with the same household, trip, row, desired state, and base version replays the originally stored successful response without another mutation/version increment. Reusing that UUID for any different request returns `409 idempotency_key_reused` without mutation. The same UUID may be used independently by a different member. Failed validation, not-found, read-only, and stale-version operations are not saved as successful operation results; reconcile the response and current row before making a new logical attempt. A concurrent matching operation may return `409 operation_in_progress`; retry the same request/UUID. The client should treat only a successful response as completion and persist enough operation state to safely retry after a lost response.

## Error envelope

API errors use `{"error":{"code":"...","message":"..."}}`; the stale-version error additionally includes top-level fields within `error`: `packed` and `version`.

| HTTP | `error.code` | Meaning / client action |
| --- | --- | --- |
| 400 | `invalid_request` | Invalid JSON shape or value; correct the request. |
| 401 | `unauthorized` | Credential missing/invalid/expired/revoked, lacking scope, no member user, or inactive user; authenticate/refresh as appropriate. |
| 404 | `not_found` | Resource missing or outside the member's current household access; do not infer which. |
| 409 | `version_conflict` | Row changed since `base_version`; use returned current `packed` and `version` to reconcile. |
| 409 | `trip_read_only` | Trip is completed/cancelled; no retry can mutate it. |
| 409 | `idempotency_key_reused` | Operation UUID belongs to a different request; do not retry under that UUID with altered data. |
| 409 | `operation_in_progress` | Matching operation is still processing; retry the exact same request and UUID. |
| 405 | (framework response) | Method not supported; reads are GET and mutation is PUT only. |

## Scope and compatibility

The mobile API exposes only household discovery, household-scoped trip listing, packing snapshots, and a single-row desired-state packing write. It does not expose pantry, meal, gear CRUD, household mutations, or bulk packing writes. Existing `/api/v1/`, web and MCP contracts remain separate and unchanged. This contract is derived from the current views, serializers, OAuth settings, models, and regression tests; OAuth Application/client provisioning and a mobile application are not part of this backend task.
