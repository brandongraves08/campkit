# CampKit — working agreement for automated contributors

These rules bind every implementer, QA reviewer, and Hermes session that changes
this repository. They exist because a defect shipped that the test suite could
never have caught: the product rule ("food belongs in Pantry, not Gear") was
never written down, so nothing asserted it and 236 green tests meant nothing
about it.

## 1. Every rule gets a test that fails when the rule breaks

- A product rule that lives only in prose, a docstring, or a chat message is not
  implemented. Write the failing test first, then make it pass.
- Cover the boundary, not the happy path: the value that must be rejected, the
  neighbouring value that must still be accepted, and the update/edit path as
  well as create. A guard that only runs on create is incomplete.
- Name the mapping explicitly in your handoff: rule → test → result
  (`rules_and_tests`).

## 2. Free text is a decision, not a default

Any field that carries a classification or business rule — `category`, `kind`,
`status`, `type`, `mode`, `unit`, `role`, and anything similar — must be in one
of these three states, and the handoff must say which:

1. **Constrained** by a vocabulary, enum, choices, or an explicit guard, with a
   test that proves the rejection.
2. **Free text by explicit decision**, with the reason recorded next to the rule
   it intentionally leaves open.
3. **Unenforced** — a defect. Do not ship it, and do not approve it.

"A CharField accepts any string" is how a business rule dies quietly.

## 3. A green suite is a coverage claim, not a correctness claim

Report test counts as what they are. Never present "N passed" as evidence that a
feature is correct when the rules were never asserted. If you find a defect the
suite could not have caught, the finding is "the rule was never specified or
never asserted", and the fix lands in both places: the spec and a failing-first
test.

## 4. Keep the invariants in the spec, not in your head

CampKit's enforced rules live under **Classification invariants** in
`docs/superpowers/specs/2026-11-01-campkit-design.md` of the lab docs
(`/root/lab-docs`). Add new rules there in the same change that adds the guard
and its test. Two currently enforced:

- Gear is durables, Pantry is consumables; `GearItem.clean()` refuses a category
  naming food (`FOOD_CATEGORY_WORDS` / `FOOD_CATEGORY_PHRASES`), while durable
  nouns (`DURABLE_CATEGORY_WORDS`) keep categories like "Food storage" valid.
- Estimates are never silently promoted; only a member acting through the
  service layer can confirm a fact, and that records actor and time.

## 5. Verification you can be held to

- **No CI.** This repository does not use GitHub Actions. Do not add workflows,
  and do not ask to enable Actions; the local suite plus QA review is the gate.
- Run the suite yourself: `python -m pytest -q` (from the repo, with the venv
  active). Report the exact command and the real count.
- Exercise the live behavior when the change is deployable; a passing unit test
  is not a deployed app. Note what you deployed and what you did not.
- Never claim a command, build, or deploy passed without its output.
