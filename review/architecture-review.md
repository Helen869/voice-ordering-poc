# Layer 1 — Architecture Review

**Objective under test:** *"Can a customer naturally place and modify a
restaurant order through a phone conversation using Dialogflow CX generative
capabilities and deterministic backend tools?"*

**Reviewer stance:** skeptical of every abstraction; the POC must stay small
enough that a single developer can hold the whole system in their head.

## Findings

### 1. Still lightweight? — YES
- Runtime deps: `fastapi`, `uvicorn`, `pydantic`, plus test-only `pytest`/`httpx`.
  No ORM, no Redis, no Pub/Sub, no vector DB, no auth framework, no frontend.
- Total backend: 5 small modules (~700 lines incl. docstrings). Dialogflow side:
  1 playbook + 5 OpenAPI files. Nothing to delete.

### 2. Unnecessary infrastructure or abstractions? — NO, with one deliberate exception
- `SessionStore` ABC + `InMemorySessionStore` is the only abstraction beyond
  plain functions. It is justified: the spec explicitly requires the storage
  seam for a future Firestore/Redis swap, and it costs ~30 lines. The seam was
  verified real — `OrderService` takes the store via constructor and never
  touches a global dict.
- Confirmation-number counter is process-local behind a thread lock.
  Deliberately NOT durable: making it durable would drag in a database and
  defeat the POC's purpose. Documented in README and code.

### 3. Conversational reasoning vs. deterministic logic — CLEANLY SEPARATED
- `app/order_service.py` owns: item existence, availability, quantity bounds,
  modifier allow-lists, required-modifier rules, price math (integer cents),
  order state machine (draft → submitted → locked).
- `dialogflow/playbook.md` owns: greeting, turn-taking, reference resolution
  ("the chicken"), clarification questions, confirmation protocol, error-code →
  speech translation.
- Routes (`main.py`) are thin: parse → service → serialize. Verified by
  reading every route; no pricing or validation logic leaked into them.

### 4. Backend authoritative for menu/prices/modifiers/availability/totals — YES
- Single source of truth: `data/menu.json`, loaded once by `MenuService`.
- Prices computed in integer cents inside `OrderService._unit_cents`;
  floats exist only on the wire. The LLM receives `unit_price`/`line_total`/
  `subtotal` and the playbook forbids it from doing arithmetic.

### 5. Could the LLM invent menu facts? — MITIGATED, not eliminated
- Guardrails: playbook "golden rule" (tools for facts), required-modifier
  ask-don't-guess rule, tool specs that describe exactly when to call
  `search_menu`, and backend 404/422 errors that punish invention with a
  correctable failure instead of a silent lie.
- Residual risk (accepted): an LLM can always hallucinate despite
  instructions. The architecture's answer is defense in depth — every
  consequential fact (what's on the order, the total, the confirmation)
  round-trips through the backend before the customer hears it. The one fact
  the backend cannot re-verify is *which item the customer meant* when speech
  is ambiguous; the playbook's "ask when ambiguous" rule + the backend's
  `ambiguous_item_reference` (409 + candidates) cover it.

### 6. Could the LLM submit without explicit confirmation? — PROCESS RISK, ACCEPTED
- The backend cannot hear the customer, so it cannot enforce "explicitness"
  itself. Enforcement lives in the playbook's mandatory 5-step confirmation
  protocol.
- Blast radius in this POC is zero: `submit_order` has no real-world side
  effects (no POS, no payment). Repeat submits are idempotent
  (`already_submitted: true`, same confirmation number). A production system
  would need a human-auditable confirmation transcript; documented as a
  remaining risk.

### 7. Storage replaceable later? — YES
- `SessionStore` ABC (`get`/`save`/`delete`) is the only contract
  `OrderService` depends on. Swapping in Firestore/Redis means writing one new
  class — no conversational or pricing logic changes.

### 8. API boundaries for future POS integration? — YES
- `submit_order` returns a complete structured order summary
  (items, quantities, modifiers, totals, confirmation number). A POS adapter
  would hook in at exactly one place: after state → submitted. No Dialogflow
  changes needed.

### 9. Hidden coupling between Dialogflow and backend? — NONE FOUND
- `session_id` is an opaque string; the backend never parses Dialogflow
  session paths. Any caller (curl, tests, e2e script) can drive the API —
  verified by `scripts/e2e.sh` running the full flow without Dialogflow.
- Two documented *convenience* behaviors could be mistaken for coupling, so
  they are specified in the tool spec, not hidden:
  (a) `add` without modifiers inherits modifiers from the unique existing
  line of the same item ("add another chicken lo mein");
  (b) `remove`/`update` selectors match by subset, so "remove the large one"
  works without naming every modifier.

## Changes made during this review
- **Partial-selector matching** (bug fix, see code review): `remove`/`update`
  with a partial modifier selector (e.g. `{"size": "large"}`) previously
  failed with `line_not_found` because selector normalization applied optional
  defaults. Selectors now validate names/options but do not apply defaults,
  and match lines by subset; multi-match returns `ambiguous_item_reference`
  with candidates. 4 regression tests added.

## Intentionally accepted POC limitations
- In-memory sessions; ephemeral Cloud Run instances; process-local
  confirmation counter (collision possible across instances).
- `--allow-unauthenticated` Cloud Run (documented; production needs IAM or API key).
- No payment, POS, auth, hours, pickup-time, delivery, multi-tenancy, CI/CD.
- Per-line quantity cap of 20.
- LLM-hallucination and premature-submit risks mitigated by process, not by code.

## Verdict
**PASS.** The implementation is as small as the problem allows, the
language/business-logic boundary is clean, and every piece of infrastructure
not needed to answer the POC question was left out.
