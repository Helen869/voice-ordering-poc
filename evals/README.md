# Live agent evals (layer 1)

Automated evals for the Gemini Live phone agent. Caller turns are sent as
**text** to the real Live model (`gemini-3.8-live`). Function calls go through
the real bridge (`live/bridge/`) to an in-process copy of the FastAPI backend.
Each run is scored with deterministic checks, so the same transcript always
gets the same verdict.

These evals call the real API and cost money or quota. They are **not** part of
`pytest`. The checks themselves are unit-tested in `tests/test_evals_checks.py`.

## Data rule for API keys

> **A free-tier key may only be used with the synthetic eval cases in
> `evals/cases.yaml`. Never use it with real call data**: no real caller
> audio, transcripts, phone numbers, names or orders.
>
> Under the Gemini API terms, content sent with a free-tier (unpaid) key may
> be used by Google to improve its products. Paid-tier
> (billed) keys are the only keys allowed anywhere real customer data is
> involved, including the production bridge and `local_harness.py` with real
> callers.

If you add a case, its caller lines must be invented, never copied from a real
call.

## Setup

```powershell
.\.venv\Scripts\python -m pip install -r evals/requirements.txt
```

Put the key in `.env` at the repo root (see `.env.example`):

| Variable | Used when |
|---|---|
| `GEMINI_EVAL_API_KEY` | Set: evals use this key, e.g. a separate free-tier key for synthetic data |
| `GEMINI_API_KEY` | Fallback when `GEMINI_EVAL_API_KEY` is unset or empty |

The runner prints which variable it used and the key's last four characters,
for example `API key: GEMINI_EVAL_API_KEY (...S4OA)`. The report records the
same, never the full key.

## Running

```powershell
# All cases, 3 runs each (defaults)
.\.venv\Scripts\python -m evals.run

# Free-tier key: one Live session at a time
.\.venv\Scripts\python -m evals.run --concurrency 1

# Selected cases, more runs
.\.venv\Scripts\python -m evals.run --runs 10 --cases barge_in_after_yes,ex6_full_order_confirm_submit
```

| Option | Default | Meaning |
|---|---|---|
| `--runs` | 3 | Runs per case |
| `--concurrency` | 3 | Live sessions at once. Use 1 on a free-tier key |
| `--threshold` | 0.8 | Minimum pass rate per case (with 3 runs, 0.8 means 3/3) |
| `--cases` | all | Comma-separated case ids |
| `--verbose` | off | Show bridge logs (tool calls, transcripts) |

Exit code: **0** means every case is at or above the threshold, **1** means
some case is below it, and **2** means the runner could not start (no key,
rejected handshake, or a bad cases file).

## Output

The console shows pass rates per case and per check, failures, warnings (for
example a repeat `submit_order` answered with `already_submitted`), and checks
that are reported for information only. A JSON report with every transcript,
function call and ordered event goes to `evals/reports/` (gitignored). It also
records the model, the key variable and suffix, and the git hash of
`live/system-instruction.md`, `live/function_declarations.json` and
`evals/cases.yaml`, with a flag if any of them had uncommitted changes.

## Cases and checks

- **Cases:** `evals/cases.yaml`. The format is documented at the top of that
  file.
- **Checks:** `evals/checks.py`. Each check's docstring says what it enforces:
  expected and forbidden calls, spoken prices taken verbatim from function
  results, no submit before an explicit yes, required modifiers never guessed,
  no leaked codes or markup, the final order in the backend, and confirmation
  numbers (correct, never early, and corrected in the same turn if wrong).
