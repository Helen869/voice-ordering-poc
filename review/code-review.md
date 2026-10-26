# Layer 2 — Code Review

Reviewed as if it were another engineer's PR: read every module, ran the
suite, probed the live API for edge cases, fixed what was wrong, re-ran.

## What was inspected
- `app/models.py`, `app/menu_service.py`, `app/order_service.py`,
  `app/session_store.py`, `app/main.py`, `data/menu.json`
- `tests/test_menu_service.py`, `tests/test_order_service.py`, `tests/test_api.py`
- `Dockerfile`, `requirements.txt`, `.env.example`, `.gitignore`
- `dialogflow/tool-specs/*.yaml` for contract drift vs. the implementation

## Findings and fixes

### BUG 1 (fixed): partial modifier selectors silently failed — `line_not_found`
**Severity:** medium. **Found by:** live API probing, not by tests.

`_resolve_single_line()` normalized a selector with `_resolve_modifiers(...,
check_required=False)`, which *applied optional defaults*. A selector like
`{"size": "large"}` became `{"size": "large", "spice_level": "medium"}` and
then exact-matched stored lines — so "remove the large General Tso's" failed
whenever the actual line had a different spice level. For a voice agent,
partial references ("the large one", "the spicy one") are the norm, not the
exception.

**Fix:** selectors now validate modifier names/options but do **not** apply
defaults, and match lines by subset (`line.modifiers ⊇ selector`).
Zero matches → `line_not_found` (404); multiple → `ambiguous_item_reference`
(409) with candidates; exactly one → target. Verified live:
- `{"size": "large"}` removed the correct line out of two ✓
- `{"spice_level": "mild"}` against two mild lines → 409 + candidates ✓
- `{"spice_level": "spicy"}` + `set_modifiers: {"spice_level": "mild"}`
  ("make the spicy one mild") ✓

**Regression tests added** (4): partial remove, ambiguous partial selector,
partial-selector update, invalid option in selector. Suite: 42 → **46 passed**.

### BUG 2 (fixed): tool-spec / playbook drifted from the real error contract
**Severity:** low (docs-only, but the LLM reads these files to decide what to
do, so wrong codes are a real conversational hazard).

- `missing_required_modifier` (singular) → actual code is
  `missing_required_modifiers`. Fixed in `playbook.md` and `update_order.yaml`.
- `order_already_submitted` → actual code is `order_locked` (409 on
  mutate-after-submit; repeat *submit* is idempotent 200, not an error).
  Fixed in both files.
- `update_order.yaml` lumped 404/409 codes under a single `422` response.
  Split into accurate `404` (`item_not_found`, `line_not_found`), `409`
  (`ambiguous_item_reference`, `order_locked`, `item_unavailable`), `422`
  (semantic validation) responses.
- `set_modifiers` was documented as "replaces the line's modifiers entirely";
  the implementation *merges* overrides onto the existing modifiers (better
  for voice: "make it spicy" keeps the size). Doc corrected.

### Non-issues reviewed and deliberately kept
- **`_unit_cents` skips unknown modifier names defensively** (`continue`).
  Unreachable in practice (lines are always normalized before pricing); kept
  as defense-in-depth, not dead code.
- **`already_submitted: Optional[bool] = None`** — present only on idempotent
  repeat submit. Asymmetric but honest; documented in the model.
- **`remove` with quantity ≥ line quantity deletes the line** rather than
  erroring. Reasonable voice semantics ("remove the egg rolls" after a partial
  miscount); documented in the tool spec.
- **`_validate_quantity` rejects bools explicitly** (`isinstance(quantity,
  bool)`) since `bool` is an `int` subclass in Python — correct paranoia for
  a JSON API.
- **Money in integer cents internally**, floats only serialized at the edge.
  No float-accumulation bugs possible; spot-checked
  `(13.99 + 3.00) × 2 = 33.98` exactly.
- **Thread lock on the confirmation counter.** Cheap insurance; the real
  limitation (process-local, cross-instance collisions) is documented, not
  papered over.
- **Structured logging**: exactly one JSON line per tool call
  (`tool`, `session_id`, `action`, `success`, `latency_ms`); no payloads, no
  PII (the POC collects none).

### Test quality
- 46 tests, all meaningful: validation errors assert the *code* and the
  *details* payload (e.g. `required_modifiers` names the missing options),
  pricing asserts exact totals, state-machine tests cover empty submit,
  double submit, and post-submit lock.
- `scripts/e2e.sh` adds 13 black-box curl checks of the full conversational
  flow (13/13 pass). No test merely re-states the implementation.

### Docker / security / hygiene
- `Dockerfile` is minimal and correct on inspection
  (`python:3.12-slim`, `$PORT` default 8080, copies only `app/` + `data/`).
  **Not buildable here — Docker is not installed in this environment.**
  Recorded as a remaining risk; the image must be built once on a machine
  with Docker before deploy.
- `requirements.txt` has 5 packages, no pins beyond floor versions — fine for
  a POC, would pin for production.
- Secret scan: clean. `.env.example` contains only `PORT`/`LOG_LEVEL`.
  `.gitignore` covers `.venv/`, `__pycache__/`, `.env`, `.pytest_cache/`.

## Verdict
**PASS after fixes.** Two real issues found (one behavioral, one contract
drift); both fixed, regression-tested, suite green. No over-engineering, no
dead code, no credentials, no business logic in routes.
