# Leak experiments: prompt, tool-result and model arms (November 2026)

**Question:** can a prompt change, a tool-result change or a different Live
model reduce leaks? A leak is internal text (reasoning, code, JSON, notes)
appearing in the agent's output transcript, and sometimes being spoken to the
caller.

**Answer:** no. Neither arm A (example format) nor arm D (other model) beats
control, and leaks appear on both Live models. We keep the current prompt and
`gemini-3.8-live`, and rely on the bridge guards (audio cut, re-answer,
no-progress watchdog) as a safety net. The root cause is not fixed.

## Setup

- **Case:** `ex2_missing_required_modifier` (two caller turns: an item that
  needs a protein, then the protein). It is synthetic and has the highest leak
  rate of the eval cases.
- **Caller:** text turns from the layer-1 eval runner (`evals/run.py`). The
  agent replies in native audio with an output transcript.
- **Key:** the free-tier eval key, with synthetic data only, at concurrency 1.
- **Dates:** 2026-11-11.
- **Code:**
  - round 1 ran on the experiments branch (`e93b96f` for control, `e36aa66`
    for the arms);
  - round 2, the confirmation runs, ran on `f02e23f`.
- **Bridge guards:** the audio-cut guard was not active in these runs. Cuts
  are simulated offline with `evals/leak_analysis.py`.
- **Batches:** round 1 is one batch of 50 runs per arm. Round 2 repeats
  control, A and D in 3 batches of 50, interleaved.

### Metrics

- **Run passes:** all checks pass for that run (`evals/checks.py`).
- **`no_leaks` failures:** runs whose transcript contains internal text.
- **Leak turns:** agent turns whose transcript matches the bridge's
  `LEAK_MARKER`, as defined in this PR. That marker includes lines starting
  with "System note", so the D numbers are counted with it.
- **Voiced:** a leak turn's audio is longer than the 90th percentile of clean
  replies to the same step, so the leaked text was likely spoken. Otherwise
  the leak is **unvoiced**: transcript only, and the caller heard a normal
  reply.

## Arms

| Arm | Change | Status |
|---|---|---|
| Control | `gemini-3.8-live`, current prompt, full tool results | 200 runs |
| A | Same model; prompt examples wrapped in `<example>` tags | 200 runs |
| B | Lower thinking budget | Not run: `gemini-3.8-live` rejects `thinking_config` |
| C | Same model and prompt; trimmed tool results | 50 runs, then dropped |
| D | `gemini-3.1-flash-live-preview`, current prompt | 200 runs, plus 20 extra runs (below) |

## Results (ex2)

| | Control | A | C | D |
|---|---|---|---|---|
| Runs | 200 | 200 | 50 | 200 |
| Runs passing | 195 (97.5%) | 195 (97.5%) | 46 (92%) | 196 (98%) [^sleep] |
| `no_leaks` failures | 3 | 5 | 2 | 3 |
| Leak turns (of 2 per run) | 4 | 6 | 4 | 3 |
| … voiced | 1 | 2 | 2 | 0 |
| Leaks with invented prices | 1 (unvoiced) | 1 (unvoiced) | 1 (voiced) | 0 |
| Other failures | 1 stalled turn [^stall], 1 server error (1011) | – | 1 guessed modifier | 1 silent turn [^silent] |
| First-audio latency, median per batch | 1.17 / 1.11 / 1.14 / 1.20 s | 1.11 / 1.09 / 1.11 / 1.19 s | 1.11 s | 1.17 / 1.08 / 1.14 / 1.09 s |
| p90 per batch | 1.55 / 1.45 / 1.47 / 2.25 s | 1.64 / 1.39 / 1.47 / 1.80 s | 1.72 s | 1.66 / 1.42 / 1.66 / 1.39 s |

[^sleep]: One more D run timed out after 12,000 s; the laptop went to sleep.
    Its reply was clean, so it was not counted as an agent failure.
[^stall]: The agent spoke its question about one and a half times, with
    unmarked self-talk. It then produced nothing for 60 s and never ended the
    turn. The new no-progress watchdog covers this case.
[^silent]: The turn contained only a "System note" self-note: no audio and
    no function call. A caller would have heard silence.

**Significance:** none of the differences are statistically significant. For
runs failing `no_leaks`, control vs A is 3/200 vs 5/200 (Fisher exact
p = 0.72), and control vs D is 3/200 vs 3/200 (p = 1.0). The earlier round-1
results, A with 1 leak and D with 0, did not hold up in the confirmation runs.

### What the leaks look like

- **`gemini-3.8-live`** (control, A and C): code fences, JSON, markdown
  bullets and plain-English reasoning after the real reply.
  - Unvoiced leaks can be very long: up to 26,000 characters, including
    made-up prices.
  - Voiced leaks ran 4–14 s past the real reply.
  - In the offline simulation, the bridge's audio cut removes 79–92% of the
    leaked audio in each voiced case. The rest plays before the marker
    arrives in the transcript.
- **`gemini-3.1-flash-live-preview`** (D): the model writes its own notes,
  starting with "System note:", at the start of the turn, followed by the
  real reply. In these runs the note was never spoken: only the real reply
  had audio.
  - The new `System note` marker catches it, at a cost. The marker fires
    before any audio, so it also mutes the correct reply that follows, and
    the caller hears a re-answer after a pause.
  - In 2 of D's 3 cases that is worse than doing nothing (3.6–4.6 s of normal
    speech cut). In the third, the silent turn, it helps.
  - This only matters if D is adopted; `gemini-3.8-live` never produced this
    text.

### D: additional runs

These were 10 runs each of the two cases most sensitive to confirmation
numbers:

| Case | Runs passing | Wrong confirmation numbers | Notes |
|---|---|---|---|
| `barge_in_after_yes` | 10 / 10 | 0 | 1 repeat submit, answered `already_submitted` (warning) |
| `ex6_full_order_confirm_submit` | 9 / 10 | 0 | 1 run dropped the 2 Cokes ($29.48 instead of $35.46) |

## Conclusions

1. **Neither A nor D beats control.** The example format and the model choice
   stay as they are; no prompt or model PR follows from these experiments.
2. **Leaks appear on both Live models**, in different forms: post-reply
   reasoning on 3.8-live, and leading self-notes on 3.1-flash. Leaked turns
   are about 1% (3.8-live: 4 of 400 control turns), and most are unvoiced.
3. **C is worse:** trimming tool results produced more voiced leaks, a
   guessed modifier and a spoken invented total. Dropped.
4. **The bridge guards are the mitigation, not a fix.** The audio cut catches
   most voiced leaks on 3.8-live. The watchdog covers the turn that stalls
   without a marker. Unmarked plain-English reasoning still gets through.
5. **Not covered here:** caller audio, rather than text, and cases other than
   ex2. Layer-3 evals with synthetic speech (`evals/tts_calls.py`) are the
   next place to measure leaks with the guards active.
