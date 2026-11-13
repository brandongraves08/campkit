# Task 4 report — trip seed/remove UI and historical read-only controls

**Status:** Implemented and locally verified; scoped commit created. Independent code review and independent visual QA are intentionally left to the controller, per task delegation instructions. No public push, live deployment, or production-data writes.

## Changes

- Added ordinary Django source-selection and seed forms. Source choices are household kits and completed trips; selected gear controls are keyed/submitted with the shared GearItem UUID and accept positive quantities through 32767. Invalid quantities and service `ValidationError`s redisplay with feedback; seed success reports added and duplicate-skipped counts.
- Added POST-only seed and association-removal routes. Both mutations call Task 2 services; removal includes CSRF and a confirmation prompt. New rows use the service’s not-packed state.
- Updated trip detail to show **Start from a kit or past trip**, add-shared-gear, pack/unpack and **Remove from this trip** only for Planned/Confirmed trips. Historical gear values, status, weight/verification labels and the Saved gear kits link remain visible. Packed/add/remove direct POST service validation failures produce message feedback and redirect instead of HTTP 500.
- Added responsive seed-row layout styles; the form remains usable without JavaScript.
- No model, migration, meal behavior, REST API, or MCP contract changes.

## TDD evidence

Before implementation, ran:

```text
/root/campkit/.venv/bin/python -m pytest tests/trips/test_trip_planning.py -q -k 'seed_trip_ui or trip_ui_remove_confirmation or seed_source_controls'
```

Observed **4 failed, 47 deselected**. Failures were the intended missing route/source UI and missing remove control (`NoReverseMatch` / absent control), not setup errors. Added implementation, then fixed issues found by these tests and browser interaction (including retaining default-selected rows after the no-JS “Choose source” POST).

## Verification

- Exact focused command from the brief:
  `/root/campkit/.venv/bin/python -m pytest tests/trips/test_trip_planning.py tests/ui/test_dashboard_views.py tests/ui/test_accessibility_smoke.py -q` → **78 passed in 25.97s**.
- Full suite: `/root/campkit/.venv/bin/python -m pytest -q` → **360 passed in 89.32s**.
- `/root/campkit/.venv/bin/python manage.py check --settings=config.settings_test` → **System check identified no issues (0 silenced).**
- `git diff --check` → passed.
- Disposable `config.settings_test` SQLite DB at `/root/.hermes/cache/scratch/task4.sqlite`, synthetic user/household/trips/kit only. Local `runserver` served the current worktree. Browser exercised source selection → seed → mark packed → accept explicit remove confirmation; read-back showed success, and the source/shared gear remained available. Completed and cancelled synthetic detail pages had no seed/add/pack/unpack/remove controls and retained the Saved gear kits link.
- Playwright measured document/body widths exactly **1440/1440** at desktop 1440×900 and **390/390** at mobile 390×844. Inspected current-build screenshots:
  - `/root/.hermes/cache/scratch/task4-qa/seed-desktop.png`
  - `/root/.hermes/cache/scratch/task4-qa/seed-mobile.png`
  - `/root/.hermes/cache/scratch/task4-qa/target-mobile.png`
  - `/root/.hermes/cache/scratch/task4-qa/completed-target.png`
  - `/root/.hermes/cache/scratch/task4-qa/cancelled-target.png`
  No horizontal overflow/clipping was observed. Mobile requires ordinary vertical scrolling to reach the lower form action. The checkbox, quantity, selectors and primary action were visible and usable; independent visual QA remains pending.

## `rules_and_tests`

- Saved kit and completed-trip choices; ordinary no-JS source-selection step; selected gear uses UUID form keys; editable positive quantities; invalid quantity redisplay: `test_seed_trip_ui_lists_sources_and_posts_gear_uuids_with_feedback`, `test_seed_trip_ui_completed_trip_source_and_empty_state`, `test_seed_source_controls_have_labels_focusable_order_and_mobile_overflow`.
- Only selected source gear added, exact success/duplicate feedback, existing target quantity/packed state preserved, new row not packed, source unchanged: `test_seed_trip_ui_lists_sources_and_posts_gear_uuids_with_feedback`, `test_seed_trip_ui_completed_trip_source_and_empty_state`.
- Actionable no-source state: `test_seed_trip_ui_completed_trip_source_and_empty_state`.
- Explicit remove confirmation, association-only removal, shared gear survives: `test_trip_ui_remove_confirmation_read_only_controls_and_direct_post_feedback`.
- Completed/cancelled display keeps history but hides source seed, manual add, pack/unpack, remove; direct add/packed/remove/seed attempts give user feedback and redirect: `test_trip_ui_remove_confirmation_read_only_controls_and_direct_post_feedback`.
- Accessible source and quantity labels, keyboard/tab order markup, responsive CSS support: `test_seed_source_controls_have_labels_focusable_order_and_mobile_overflow`; existing `tests/ui/test_accessibility_smoke.py` also passed in the focused run.
- Cross-cutting read-only service enforcement and completed/cancelled service rejection remain covered by Task 2 service tests in `tests/trips/test_trip_planning.py`.

## Scope, commit, and remaining gate

- Commit subject: `feat: seed and manage trip packing lists` (scoped commit; see branch HEAD).
- Preserved pre-existing untracked `task-artifacts-pantrypolish/`; it is not staged.
- No public API/MCP changes, push, deployment, or production writes.
- **Remaining:** independent reviewer/controller visual QA. No reviewer was dispatched by this implementer.
