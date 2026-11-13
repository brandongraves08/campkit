# Task 5 report — mobile API contract and regressions

## Scope and base

- Branch: `feat/campkit-mobile-backend-20261110`.
- Starting HEAD/base before Task 5: `9fe43c0958b88e3614fc736fdc91700f0ebfefdd` (reviewed Task 4 candidate).
- Verified Task 4 and prior work are in history: Task 4 candidate `9fe43c0` follows `c3897ae` (row-version/idempotent sync); prior mobile auth commits include inactive-user rejection `d1bbb9a`, user-less-token rejection `c5dde45`, and scoped mobile reads `10d1bdf`. The merge base with the reviewed pre-Task-4 security base `c5dde455b779134318d32480467e847d3d132a42` is exactly that SHA.
- Existing unrelated untracked paths `docs/superpowers/`, `task-artifacts-pantrypolish/`, and `uv.lock` were observed and left untouched.
- No production records, OAuth Application record, deployment, public push, or external publication were created/performed.

## Changes

- Added `docs/mobile-api-contract.md`, documenting the implemented OAuth authorization-code + PKCE configuration, exact native callback, scope/token limits, private-network HTTPS/base URL, route paths and response fields, household authorization semantics, request validation, row version and idempotency behavior, stable errors, and scope/compatibility limits.
- Added a short `Native mobile API` section to `deploy/README.md` linking the contract and noting private LAN/VPN use, PKCE, no OAuth provisioning/app/deployment, and separation from existing `/api/v1/`, web and MCP contracts.
- Documentation is derived from `config/settings.py`, `apps/mobile_api` routes/auth/views/serializers/models, trip version service, and existing regression tests. No implementation behavior or test code was changed in this documentation task.

## Rules → tests → observed results

The existing tests form regression evidence for the documented contract; the Task 5 change itself is docs-only and did not introduce separate behavior requiring new tests.

- Native authorization uses the public authorization-code flow, exact redirect URI, required S256 PKCE, only `mobile:packing`; unsupported grants and redirect/PKCE violations are rejected → `tests/mobile_api/test_oauth_pkce.py::test_provider_settings_are_minimal_and_secure`, `test_public_client_is_authorization_code_only_and_has_exact_callback`, `test_authorization_uses_existing_member_browser_login`, `test_authorization_rejects_unregistered_redirect_uri`, `test_authorization_rejects_missing_or_plain_pkce`, `test_token_endpoint_rejects_unapproved_grant_types`, `test_authorize_endpoint_rejects_implicit_response`, `test_authorization_issues_mobile_scope_and_rejects_unregistered_scope` → all passed in focused and full suites.
- Bearer auth requires valid, unexpired, unrevoked, correctly scoped token for active member user; missing/invalid/expired/revoked/underscoped/userless/inactive credentials fail uniformly → `tests/mobile_api/test_households_and_trip_snapshots.py::test_invalid_credentials_share_401_and_do_not_write`, `test_deactivated_user_cannot_read_mobile_resources_without_writes`, `test_token_without_member_user_cannot_read_mobile_resources_without_writes`; `tests/mobile_api/test_packing_sync.py::test_packing_write_rejects_invalid_member_credentials_without_mutation` → passed.
- Household list is member-only; household/trip membership boundaries and revoked-membership behavior do not reveal foreign/missing resources → `test_household_discovery_returns_only_member_households_and_empty_list`, `test_revoked_membership_denies_household_trip_and_packing_reads`, `test_foreign_missing_and_malformed_ids_are_indistinguishable`, `test_packing_write_requires_active_membership_without_mutation_after_revocation` → passed.
- Read endpoints are GET-only, snapshots and trip results expose only exact allowlisted fields, including row version; empty results are valid → `test_mobile_read_endpoints_reject_non_get_methods`, `test_trip_list_and_packing_snapshot_have_only_contract_fields`, `test_empty_trip_and_packing_lists_are_empty`, `test_foreign_household_gear_never_leaks_into_snapshot` → passed.
- Packing writes are PUT-only; malformed shape/types/UUID/non-positive version are rejected without mutation; only active planned-trip rows are mutable → `tests/mobile_api/test_packing_sync.py::test_packing_sync_accepts_only_put`, `test_invalid_sync_shape_is_rejected_without_mutation`, `test_completed_or_cancelled_trip_returns_stable_read_only_error`, `tests/trips/test_trip_gear_versions.py::test_read_only_trip_rejects_versioned_and_legacy_writes` → passed.
- Versions default to 1, increment only for actual change and are per row; stale version returns current state without mutation; legacy web writes remain compatible → `tests/trips/test_trip_gear_versions.py::test_trip_gear_starts_at_version_one_and_mutations_increment_only_changed_row`, `test_repeating_desired_state_at_current_version_is_a_noop`, `test_distinct_packing_rows_have_independent_versions`, `test_stale_row_version_raises_conflict_without_mutation`, `tests/mobile_api/test_packing_sync.py::test_stale_base_version_returns_current_state_and_does_not_mutate`, `tests/trips/test_trip_gear_versions.py::test_legacy_web_write_remains_compatible_and_advances_row_version` → passed.
- Successful same-member/key/request retry replays the saved result; changed request under same key conflicts without mutation; keys are scoped per member → `tests/mobile_api/test_packing_sync.py::test_new_operation_updates_state_once_and_same_request_replays_original_result`, `test_same_operation_id_with_changed_payload_conflicts_without_mutation`, `test_operation_idempotency_is_scoped_per_member` → passed.
- Existing `/api/v1/`, web and MCP contracts remain intact → regressions in `tests/api`, `tests/trips`, and `tests/mcp`, plus mobile tests, all passed (command below). No existing interface code was changed by Task 5.

## Commands and actual results

- `git diff --check` — exit 0; no whitespace errors (run before regression suite and again after drafting).
- `.venv/bin/python -m pytest tests/mobile_api tests/api tests/trips tests/mcp -q` — **156 passed in 79.84s**, exit 0.
- `.venv/bin/python -m pytest -q` — **433 passed in 167.49s**, exit 0.
- No UI changed, so visual UI inspection is not applicable. No deployment or external state change occurred.

## Limitations

OAuth Application/client provisioning and the mobile application remain out of scope. The service should remain private to LAN/VPN HTTPS; this work does not deploy it. The full local suite passed in this run; no CI, push, PR, or remote validation was performed or claimed.
