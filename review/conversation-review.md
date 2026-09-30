# Layer 3 — Conversation / Agent Review

**Question:** can the Playbook + tool design carry realistic restaurant
conversations? Each scenario below was walked against the live backend
(`scripts/e2e.sh` covers the core path; the rest were probed with curl).
For every scenario: expected tool calls, expected order state, failure modes,
and whether the current prompt/tool design handles it.

Conventions: `S-*` = session id. Prices from the real menu (verified).

---

## Scenario A — modify, re-total, confirm, submit
**Script:** "I'd like two egg rolls and General Tso's chicken." → "Make the
chicken spicy." → "Actually just one egg roll." → "What's my total?" →
"Yes, place the order."

**Expected tool calls & state** (verified live):
1. `update_order{add, egg-rolls, qty 2}` → subtotal 3.50
2. `update_order{add, general-tsos-chicken}` → **422
   `missing_required_modifiers`** (size required) → agent asks
   "regular or large?" → customer: "large" →
   `update_order{add, general-tsos-chicken, {size: large}}` → 20.49
3. "Make the chicken spicy" — exactly one chicken line on the order, so
   `update_order{update, general-tsos-chicken, set_modifiers: {spice_level:
   spicy}}` (merge keeps size=large) → 20.49
4. "Actually just one egg roll" →
   `update_order{update, egg-rolls, quantity: 1}` → **18.74**
5. "What's my total?" → `get_order` → quote **18.74**
6. "Yes, place the order" → `submit_order` → `GW-1042`, state submitted

**Failure modes:** if the agent guessed a size at step 2 instead of asking,
the order would contain an unconfirmed $3.00 large upcharge. The playbook's
ask-don't-guess rule plus the backend's hard 422 make guessing both
forbidden and impossible to get away with. **Handled.**

## Scenario B — missing required modifier, context carry
**Script:** "I want fried rice." → "Shrimp."

**Expected:** `update_order{add, fried-rice}` → 422
`missing_required_modifiers` with `details.required_modifiers` naming protein
+ options → agent asks "vegetable, chicken, beef, or shrimp?" → "Shrimp" →
`update_order{add, fried-rice, {protein: shrimp}}` → unit 12.49. (Verified
live.) The one-word follow-up resolves entirely from the pending clarification
— no extra state needed. **Handled.**

## Scenario C — availability question, no invention
**Script:** "Do you have beef noodles?"

**Expected:** `search_menu{q: "beef noodles"}`. Substring pass: "beef"
matches beef-broccoli and the protein=beef option text indexed for lo-mein /
chow-mein / fried-rice. Agent answers from matches: "We don't have a dish
called beef noodles, but we have beef lo mein, beef chow mein, and beef fried
rice." **Handled** — and the negative case ("cheeseburger" → 0 matches →
polite decline + real alternatives) is Example 5 in the playbook.

**Failure mode:** the fuzzy fallback (cutoff 0.7, token-level) was tuned so
"cheeseburger" does NOT fuzzy-match "Crab Rangoons" (0.67 < 0.7 — measured
during development), while "chiken" still finds chicken items (0.92).
Documented in `menu_service.py`.

## Scenario D — ambiguous reference ("make the chicken spicy")
**Script:** "I want General Tso's chicken and chicken lo mein." → "Make the
chicken spicy."

**Expected:** TWO chicken lines exist. The playbook's reference-resolution
rule (step 4) requires the agent to **ask** which one — never silently pick.
If the agent instead calls
`update_order{update, item_id: ???}` it cannot even form the call without
choosing an id, which is exactly the forcing function: the honest move is to
ask, "Did you mean the General Tso's chicken or the chicken lo mein?" If it
does guess an id and the customer corrects it, the backend state is still
consistent (only one line changed) and recoverable.

**Residual risk (accepted):** a lazy agent could silently pick. Mitigation is
prompt-level, not enforceable in code. The playbook makes the rule explicit
and gives the exact clarification phrasing; the backend's
`ambiguous_item_reference` covers the *selector* variant of the same problem
(e.g. two General Tso's lines, "make the spicy one mild" when both are mild
→ 409 + candidates → agent asks). **Handled as well as a prompt can.**

## Scenario E — quantity oscillation
**Script:** "Two egg rolls." → "Actually make that three." → "No, two was right."

**Expected:** add qty 2 → 3.50; `update{quantity: 3}` → 5.25;
`update{quantity: 2}` → 3.50. (Verified live.) Because `update` sets an
absolute quantity (not a delta), oscillation is idempotent and cannot drift.
The playbook documents "quantity is the NEW total, not a delta" with the egg
roll example. **Handled.**

## Scenario F — "That's all" must NOT submit
**Script:** order built, customer: "That's all."

**Expected:** agent calls `get_order`, reads back concise summary + subtotal,
asks "Would you like me to place the order?" — and does **not** call
`submit_order`. The playbook's confirmation protocol step 1–3 is explicit:
"That's all" / "That's it" / vague "okay" are listed as NOT confirmation.
**Handled** at the prompt level; backend cannot distinguish, which is why the
rule is stated as a mandatory protocol, not a suggestion.

## Scenario G — change during final confirmation
**Script:** confirmation read back → customer: "Actually remove the Cokes,
add a Sprite." → agent must update, re-fetch, re-read, re-ask.

**Expected:** `update_order{remove, coke}` → `update_order{add, sprite}` →
`get_order` (new subtotal) → revised summary → "Would you like me to place
the order?" again → only then `submit_order`. (Verified live: 18.47 → 12.49
→ 15.48 → submitted GW-1043.) Protocol step 4 covers this exact loop.
**Handled.**

## Scenario H — unavailable item, no hallucination
**Script:** "Can I get a cheeseburger?" / "Make the egg roll extra spicy."

**Expected:** `search_menu{q: "cheeseburger"}` → 0 matches → "Sorry, we don't
have cheeseburgers…" + real alternatives. `update_order{add, egg-rolls,
{spice_level: extra-hot}}` → 422 `unknown_modifier` → agent: "Egg rolls don't
come with a spice level — did you want something else?" (Verified live.)
The error-code → speech table gives the agent words for every failure.
**Handled.**

---

## Cross-scenario notes
- **Reference resolution toolkit** the agent actually has: conversation memory
  + `get_order` (ground truth of what's on the order) + subset selectors on
  `remove`/`update` (fixed in code review: "the large one" works) +
  `ambiguous_item_reference` candidates (tells the agent exactly what to ask
  about). This is a good balance — the API does the deterministic matching,
  the LLM does the asking.
- **Money discipline:** every price the customer hears comes from a tool
  response field (`unit_price`, `line_total`, `subtotal`). The playbook
  forbids agent arithmetic; the tool specs repeat it. Spot-checked all six
  few-shot examples' arithmetic against the menu — all correct.
- **What the design cannot do:** detect sarcasm/negation edge cases ("don't
  add the..."), handle two callers on one line, or recover a session after a
  Cloud Run restart. All out of scope for the POC question by design.
