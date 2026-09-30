# Self Review Summary

Three-layer self-review + adversarial pass performed 2026-09-30 against the
live backend (pytest + curl probing + `scripts/e2e.sh`).

## Architecture
- **PASS**
- Major findings: none structural. The implementation is as small as the
  problem allows (5 backend modules, 5 deps, no infra beyond Cloud Run);
  language understanding and business logic are cleanly separated; the backend
  is authoritative for all menu/order facts; `SessionStore` ABC gives a real,
  minimal seam for a future Firestore/Redis swap; `submit_order` is the single
  future POS hook point.
- One behavioral fix came out of this layer's probing (partial modifier
  selectors) — fixed, regression-tested.

## Code
- **PASS** (after fixes)
- Major findings:
  1. Partial-selector default-application bug → fixed (see Bugs below).
  2. Tool-spec/playbook error-code drift (`missing_required_modifiers`,
     `order_locked`, 404/409/422 mapping, `set_modifiers` merge semantics) →
     corrected in `update_order.yaml` and `playbook.md`.
- Non-issues kept deliberately: integer-cents pricing, bool-quantity guard,
  thread-locked confirmation counter, one JSON log line per tool call, no PII.

## Conversation Design
- **PASS**
- Major findings: scenarios A–H all walk correctly against the live API.
  Reference resolution has a genuine toolkit (conversation memory +
  `get_order` ground truth + subset selectors + `ambiguous_item_reference`
  candidates). All six few-shot example totals verified against the real menu.
  Two residual prompt-level risks (LLM hallucination despite instructions;
  silent disambiguation guess) are mitigated by process, not code — accepted
  for a POC with zero real-world side effects.

## Bugs Found During Self Review
| # | Bug | How found | Fix |
|---|---|---|---|
| 1 | `remove`/`update` with a partial modifier selector (e.g. `{"size":"large"}`) failed with `line_not_found` because selector normalization applied optional defaults | Live API probing (tests didn't cover it) | Selectors validate names/options but skip defaults; match lines by subset; multi-match → `ambiguous_item_reference` + candidates |
| 2 | `update_order.yaml` + `playbook.md` used wrong error codes (`missing_required_modifier` singular, `order_already_submitted`) and lumped 404/409 codes under 422 | Contract diff vs `order_service.py` | Corrected codes, split responses into 404/409/422, fixed `set_modifiers` merge-semantics doc |

## Remaining POC Risks
- **Docker image never built** — Docker is not installed in this sandbox.
  The Dockerfile is minimal and inspected-clean, but it must be built once
  (by Helen or CI) before `gcloud builds submit` / deploy.
- **Cloud Run ephemerality** — in-memory sessions die on restart; concurrent
  instances don't share sessions and the `GW-####` counter can collide across
  instances. By design for the POC; documented in README.
- **`--allow-unauthenticated`** deploy (documented tradeoff; production needs
  IAM invoker or API key).
- **Prompt-level risks** — LLM hallucination / premature submit can't be
  enforced in code; mitigated by playbook protocol + idempotent, side-effect-free
  `submit_order`.
- **Phone Gateway + real Dialogflow wiring untested** — manual steps in README;
  simulator testing is the gate before enabling telephony.

## Test results
- `pytest`: **65 passed** (9 menu + 29 order-service + 8 API + 19 adversarial),
  0 failed.
- `scripts/e2e.sh`: **13/13** black-box curl checks pass.
- App starts cleanly via uvicorn; `/health` + tool endpoints verified live.

## Recommendation
**Ready for external review.** Merge is intentionally not requested — the
branch `feature/voice-ordering-poc` is pushed for review only. Before any
production use: build the Docker image, replace the session store, add auth,
and run the Dialogflow simulator + phone tests in README.
