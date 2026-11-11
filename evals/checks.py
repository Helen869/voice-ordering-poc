"""Deterministic checks for Live agent eval runs (no API access, no randomness).

A run record (built by evals/run.py) looks like::

    {
      "steps": [
        {"say": "...", "explicit_yes": bool,
         "agent": "<concatenated output transcript for this step>",
         "calls": [{"name": ..., "args": {...}, "result": {...}}, ...]},
        ...
      ],
      "final_order": {...get_order summary...},
      "error": None | "<why the run could not complete>",
    }

Each check returns (passed, detail) or None when it does not apply to the case.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from live.bridge.call import SPOKEN_CONFIRMATION

MENU_PATH = Path(__file__).resolve().parent.parent / "data" / "menu.json"

# Price fields a spoken amount may be quoted from, verbatim (never a computed sum).
PRICE_FIELDS = ("subtotal", "unit_price", "line_total", "base_price", "price_delta")

# Error codes whose spoken form would never occur in natural speech.
LEAK_CODES = (
    "item_not_found", "line_not_found", "missing_required_modifiers",
    "invalid_modifier_option", "unknown_modifier", "ambiguous_item_reference",
    "backend_unavailable", "invalid_arguments", "nothing_to_update",
    "invalid_request", "missing_query",
)

CheckResult = Optional[tuple[bool, str]]


# ---------------------------------------------------------------------------
# Spoken money amounts
# ---------------------------------------------------------------------------
_UNITS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_SENTENCE_SPLIT = re.compile(r"[.?!](?=\s|$)")  # never inside "12.49"


def _number(tokens: list[str], i: int) -> tuple[Optional[int], int]:
    """Parse 0-99 from words (or digits) at tokens[i]; return (value, next index)."""
    if i >= len(tokens):
        return None, i
    tok = tokens[i]
    if tok.isdigit() and len(tok) <= 2:
        return int(tok), i + 1
    if tok in _TENS:
        value = _TENS[tok]
        if i + 1 < len(tokens) and tokens[i + 1] in _UNITS and 0 < _UNITS[tokens[i + 1]] < 10:
            return value + _UNITS[tokens[i + 1]], i + 2
        return value, i + 1
    if tok in _UNITS and tok != "oh":
        return _UNITS[tok], i + 1
    return None, i


def _word_amounts(sentence: str) -> list[float]:
    tokens = re.sub(r"[^a-z0-9 ]", " ", sentence.lower().replace("-", " ")).split()
    found, i = [], 0
    while i < len(tokens):
        dollars, j = _number(tokens, i)
        if dollars is None:
            i += 1
            continue
        said_dollars = j < len(tokens) and tokens[j] in ("dollar", "dollars", "bucks")
        if said_dollars:
            j += 1
            if j < len(tokens) and tokens[j] == "and":
                j += 1
        cents, k = _number(tokens, j)
        if cents is not None and k < len(tokens) and tokens[k] in ("cent", "cents"):
            k += 1
        if said_dollars:
            found.append(dollars + (cents or 0) / 100)
            i = k if cents is not None else j
        elif cents is not None and dollars >= 1 and cents >= 10:
            # "eleven forty nine", "twenty six ninety eight"
            found.append(dollars + cents / 100)
            i = k
        else:
            i += 1
    return found


def spoken_amounts(text: str) -> list[float]:
    """Dollar amounts in a transcript: "$12.49", "12.49", "twelve forty-nine",
    "eleven dollars and forty-nine cents". Sentences mentioning a confirmation
    number are skipped (GW-1042 is not a price)."""
    amounts: list[float] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        if "confirmation" in sentence.lower():
            continue
        digit_spans = []
        for m in re.finditer(r"\$\s?(\d+(?:\.\d{1,2})?)|\b(\d+\.\d{2})\b", sentence):
            amounts.append(float(m.group(1) or m.group(2)))
            digit_spans.append(m.span())
        for start, end in reversed(digit_spans):
            sentence = sentence[:start] + " " + sentence[end:]
        amounts.extend(_word_amounts(sentence))
    return [round(a, 2) for a in amounts]


def price_values(result: Any) -> set[float]:
    """Every PRICE_FIELDS value anywhere in a tool result (incl. modifier option prices)."""
    found: set[float] = set()
    if isinstance(result, dict):
        for key, value in result.items():
            if key in PRICE_FIELDS and isinstance(value, (int, float)):
                found.add(round(float(value), 2))
            else:
                found |= price_values(value)
    elif isinstance(result, list):
        for value in result:
            found |= price_values(value)
    return found


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_menu(path: Path = MENU_PATH) -> dict[str, dict]:
    return {item["id"]: item for item in json.loads(path.read_text(encoding="utf-8"))["items"]}


def modifier_dict(value: Any) -> dict:
    """Model args use [{name, value}]; expectations and the backend use dicts."""
    if isinstance(value, list):
        return {p.get("name"): p.get("value") for p in value if isinstance(p, dict)}
    return dict(value or {})


def _all_calls(run: dict) -> Iterable[tuple[int, dict]]:
    for index, step in enumerate(run["steps"]):
        for call in step["calls"]:
            yield index, call


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("-", " ")).strip()


def _args_match(expected: dict, actual: dict) -> bool:
    for key, want in expected.items():
        got = actual.get(key)
        if key in ("modifiers", "set_modifiers"):
            have = modifier_dict(got)
            if any(have.get(k) != v for k, v in modifier_dict(want).items()):
                return False
        elif key == "q":
            if not isinstance(got, str) or str(want).lower() not in got.lower():
                return False
        elif got != want and not (isinstance(want, int) and got == float(want)):
            return False
    return True


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_completed(case: dict, run: dict) -> CheckResult:
    if run.get("error"):
        return False, run["error"]
    return True, "all steps completed"


def check_expected_calls(case: dict, run: dict) -> CheckResult:
    """Each expected call is matched by a distinct actual call (any order)."""
    expected = case.get("expect_calls") or []
    if not expected:
        return None
    unused = [call for _, call in _all_calls(run)]
    missing = []
    for exp in expected:
        match = next(
            (c for c in unused if c["name"] == exp["name"] and _args_match(exp.get("args", {}), c["args"])),
            None,
        )
        if match is None:
            missing.append(f"{exp['name']}({exp.get('args', {})})")
        else:
            unused.remove(match)
    return (not missing), ("all matched" if not missing else "missing: " + "; ".join(missing))


def check_forbidden_calls(case: dict, run: dict) -> CheckResult:
    rules = [(i, s["forbid_calls"]) for i, s in enumerate(case["steps"]) if s.get("forbid_calls")]
    if not rules:
        return None
    bad = []
    for index, names in rules:
        if index < len(run["steps"]):
            bad += [f"step {index + 1}: {c['name']}" for c in run["steps"][index]["calls"] if c["name"] in names]
    return (not bad), ("none" if not bad else ", ".join(bad))


def check_money_from_tools(case: dict, run: dict) -> CheckResult:
    """Every spoken dollar amount appears verbatim in a price field of an earlier
    tool result. Sums the agent computed itself (not in any result) fail."""
    allowed: set[float] = set()
    bad, spoken = [], 0
    for index, step in enumerate(run["steps"]):
        for call in step["calls"]:
            allowed |= price_values(call.get("result"))
        for amount in spoken_amounts(step["agent"]):
            spoken += 1
            if amount not in allowed:
                bad.append(f"step {index + 1}: ${amount:.2f}")
    if bad:
        return False, "not from a tool result: " + ", ".join(bad)
    return True, f"{spoken} amount(s) verified"


def check_no_premature_submit(case: dict, run: dict) -> CheckResult:
    yes_steps = [i for i, s in enumerate(case["steps"]) if s.get("explicit_yes")]
    first_yes = yes_steps[0] if yes_steps else None
    early = [i for i, c in _all_calls(run) if c["name"] == "submit_order" and (first_yes is None or i < first_yes)]
    if early:
        return False, f"submit_order at step(s) {sorted({i + 1 for i in early})} before an explicit yes"
    return True, "ok"


def check_required_modifiers_from_caller(case: dict, run: dict, menu: Optional[dict] = None) -> CheckResult:
    """A required-modifier value sent to update_order must have been said by the
    caller (volunteered, or in answer to the agent's question): never guessed."""
    menu = menu or load_menu()
    caller_text, checked, bad = "", 0, []
    for index, step in enumerate(run["steps"]):
        caller_text += " " + _norm(step["say"])
        for call in step["calls"]:
            if call["name"] != "update_order":
                continue
            item = menu.get(call["args"].get("item_id"))
            if not item:
                continue
            required = {g["name"] for g in item["modifiers"] if g.get("required")}
            sent = {**modifier_dict(call["args"].get("modifiers")), **modifier_dict(call["args"].get("set_modifiers"))}
            for name in required & set(sent):
                checked += 1
                value = str(sent[name])
                if not re.search(rf"\b{re.escape(_norm(value))}\b", caller_text):
                    bad.append(f"step {index + 1}: {item['id']} {name}={value}")
    if not checked:
        return None
    return (not bad), ("ok" if not bad else "guessed: " + ", ".join(bad))


def check_speech(case: dict, run: dict) -> CheckResult:
    """Per-step say_all / say_any / say_none substrings (case-insensitive)."""
    problems, rules = [], 0
    for index, spec in enumerate(case["steps"]):
        if index >= len(run["steps"]):
            break
        text = _norm(run["steps"][index]["agent"])
        for want in spec.get("say_all", []):
            rules += 1
            if _norm(want) not in text:
                problems.append(f"step {index + 1}: missing {want!r}")
        if spec.get("say_any"):
            rules += 1
            if not any(_norm(w) in text for w in spec["say_any"]):
                problems.append(f"step {index + 1}: none of {spec['say_any']}")
        for bad in spec.get("say_none", []):
            rules += 1
            if _norm(bad) in text:
                problems.append(f"step {index + 1}: said {bad!r}")
    if not rules:
        return None
    return (not problems), ("ok" if not problems else "; ".join(problems))


def check_no_leaks(case: dict, run: dict) -> CheckResult:
    """No error codes, JSON or system notes read aloud."""
    text = " ".join(step["agent"] for step in run["steps"])
    low = text.lower()
    leaks = [c for c in LEAK_CODES if c in low or c.replace("_", " ") in low]
    if "system note" in low:
        leaks.append("System note")
    if "{" in text or "}" in text:
        leaks.append("JSON braces")
    return (not leaks), ("ok" if not leaks else "spoke: " + ", ".join(leaks))


def _confirmation_forms(number: str) -> list[str]:
    digits = re.sub(r"\D", "", number)
    names = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
    forms = [digits, " ".join(names[int(d)] for d in digits)]
    if len(digits) == 4:
        def two(n: int) -> str:
            if n < 20:
                return [k for k, v in _UNITS.items() if v == n and k != "oh"][0]
            tens = [k for k, v in _TENS.items() if v == n - n % 10][0]
            return tens + (" " + names[n % 10] if n % 10 else "")
        forms.append(f"{two(int(digits[:2]))} {two(int(digits[2:]))}")
    return forms


def check_final_order(case: dict, run: dict) -> CheckResult:
    exp = case.get("final")
    if exp is None:
        return None
    order = run.get("final_order") or {}
    problems = []
    if "state" in exp and order.get("state") != exp["state"]:
        problems.append(f"state {order.get('state')!r} != {exp['state']!r}")
    if "subtotal" in exp and round(float(order.get("subtotal", -1)), 2) != round(float(exp["subtotal"]), 2):
        problems.append(f"subtotal {order.get('subtotal')} != {exp['subtotal']}")
    if "items" in exp:
        actual = list(order.get("items", []))
        for want in exp["items"]:
            match = next(
                (a for a in actual if a["item_id"] == want["item_id"]
                 and a["quantity"] == want.get("quantity", a["quantity"])
                 and all(a["modifiers"].get(k) == v for k, v in want.get("modifiers", {}).items())),
                None,
            )
            if match is None:
                problems.append(f"missing {want}")
            else:
                actual.remove(match)
        problems += [f"unexpected {a['item_id']} x{a['quantity']} {a['modifiers']}" for a in actual]
    if exp.get("state") == "submitted" and not order.get("confirmation_number"):
        problems.append("no confirmation number")
    return (not problems), ("ok" if not problems else "; ".join(problems))


def check_confirmation_spoken(case: dict, run: dict) -> CheckResult:
    """A submitted order's confirmation number was read to the caller."""
    number = (run.get("final_order") or {}).get("confirmation_number")
    if not number:
        return None
    spoken = _norm(" ".join(step["agent"] for step in run["steps"]))
    if any(form in spoken for form in _confirmation_forms(number)):
        return True, "ok"
    return False, f"confirmation {number} never spoken"


_SPOKEN_CONFIRMATION = SPOKEN_CONFIRMATION  # one definition, shared with the bridge guard


def _successful_submits(run: dict) -> list[dict]:
    return [c["result"] for _, c in _all_calls(run)
            if c["name"] == "submit_order" and "error" not in c["result"]
            and c["result"].get("state") == "submitted" and c["result"].get("confirmation_number")]


def check_confirmation_numbers(case: dict, run: dict) -> CheckResult:
    """Every spoken confirmation number equals one submit_order returned, and is
    spoken only after that result arrived (by event order). Digit forms only:
    a number read as words is not detected here."""
    events = run.get("events")
    if not events:
        return None
    text, ends = "", []  # concatenated agent speech; (end offset, seq) per chunk
    returned: dict[str, int] = {}  # digits -> seq of the first result returning it
    for ev in events:
        if ev["type"] == "agent_text":
            text += ev["text"]
            ends.append((len(text), ev["seq"]))
        elif (ev["type"] == "tool_result" and ev.get("name") == "submit_order"
              and "error" not in ev["result"] and ev["result"].get("confirmation_number")):
            digits = re.sub(r"\D", "", ev["result"]["confirmation_number"])
            returned.setdefault(digits, ev["seq"])
    spoken = []
    for m in _SPOKEN_CONFIRMATION.finditer(text):
        digits = re.sub(r"\D", "", m.group(1) or m.group(2))
        seq = next(s for end, s in ends if end >= m.end())  # chunk that completed it
        spoken.append((digits, seq))
    if not spoken and not returned:
        return None
    problems = []
    for digits, seq in spoken:
        if digits not in returned:
            problems.append(f"spoke GW-{digits}, but submit_order returned "
                            f"{', '.join('GW-' + d for d in returned) or 'none'}")
        elif seq < returned[digits]:
            problems.append(f"spoke GW-{digits} before submit_order returned it")
    return (not problems), ("ok: " + ", ".join(f"GW-{d}" for d, _ in spoken) if not problems
                            else "; ".join(problems))


def _agent_mentions(run: dict) -> tuple[list[tuple[str, int, int]], dict[str, int]]:
    """([(digits, seq, agent turn)], {returned digits: seq}). An agent turn is
    the agent's speech between two caller turns."""
    text, ends, returned, turn = "", [], {}, 0
    for ev in run.get("events") or []:
        if ev["type"] == "caller":
            turn += 1
            text += "\n"
            ends.append((len(text), ev["seq"], turn))
        elif ev["type"] == "agent_text":
            text += ev["text"]
            ends.append((len(text), ev["seq"], turn))
        elif (ev["type"] == "tool_result" and ev.get("name") == "submit_order"
              and "error" not in ev["result"] and ev["result"].get("confirmation_number")):
            returned.setdefault(re.sub(r"\D", "", ev["result"]["confirmation_number"]), ev["seq"])
    mentions = []
    for m in SPOKEN_CONFIRMATION.finditer(text):
        digits = re.sub(r"\D", "", m.group(1) or m.group(2))
        seq, turn_of = next((s, t) for end, s, t in ends if end >= m.end())
        mentions.append((digits, seq, turn_of))
    return mentions, returned


def check_wrong_number_corrected(case: dict, run: dict) -> CheckResult:
    """Every wrong (or early) confirmation number is followed, in the same
    agent turn, by the real one spoken after submit_order returned it."""
    mentions, returned = _agent_mentions(run)
    wrong = [(d, s, t) for d, s, t in mentions if d not in returned or s < returned[d]]
    if not wrong:
        return None
    uncorrected = []
    for digits, seq, turn in wrong:
        fixed = any(d in returned and s > seq and s > returned[d] and t == turn for d, s, t in mentions)
        if not fixed:
            uncorrected.append(f"GW-{digits}")
    if uncorrected:
        return False, "not corrected in the same agent turn: " + ", ".join(uncorrected)
    return True, "corrected: " + ", ".join(f"GW-{d}" for d, _, _ in wrong)


def check_get_order_before_submit(case: dict, run: dict) -> CheckResult:
    """Every submit_order that reached the backend came after a successful
    get_order with no successful update_order in between (attempts the bridge
    refused with invalid_arguments are counted in the detail only)."""
    reviewed, executed, refused, bad = False, 0, 0, []
    for index, call in _all_calls(run):
        ok = "error" not in call["result"]
        if call["name"] == "update_order" and ok:
            reviewed = False
        elif call["name"] == "get_order" and ok:
            reviewed = True
        elif call["name"] == "submit_order":
            if call["result"].get("error", {}).get("code") == "invalid_arguments":
                refused += 1
                continue
            executed += 1
            if not reviewed:
                bad.append(f"step {index + 1}")
    if not executed and not refused:
        return None
    note = f" ({refused} attempt(s) refused by the bridge)" if refused else ""
    if bad:
        return False, "submit_order without a prior get_order at " + ", ".join(bad) + note
    return True, f"ok{note}"


def check_submit_at_most_once(case: dict, run: dict) -> CheckResult:
    """One successful submit_order per order (a repeat returns already_submitted)."""
    submits = _successful_submits(run)
    if not submits:
        return None
    counts: dict[str, int] = {}
    for result in submits:
        counts[result["confirmation_number"]] = counts.get(result["confirmation_number"], 0) + 1
    repeated = {k: v for k, v in counts.items() if v > 1}
    return (not repeated), ("ok" if not repeated else
                            "; ".join(f"{k} submitted {v} times" for k, v in repeated.items()))


CHECKS = {
    "completed": check_completed,
    "expected_calls": check_expected_calls,
    "forbidden_calls": check_forbidden_calls,
    "money_from_tools": check_money_from_tools,
    "no_premature_submit": check_no_premature_submit,
    "required_modifiers_from_caller": check_required_modifiers_from_caller,
    "speech": check_speech,
    "no_leaks": check_no_leaks,
    "final_order": check_final_order,
    "confirmation_spoken": check_confirmation_spoken,
    "confirmation_numbers": check_confirmation_numbers,
    "wrong_number_corrected": check_wrong_number_corrected,
    "submit_at_most_once": check_submit_at_most_once,
    "get_order_before_submit": check_get_order_before_submit,
}


def evaluate(case: dict, run: dict) -> dict[str, dict]:
    """Run every applicable check: {name: {"passed", "detail", "counted"}}.
    A case's ``only_checks`` list decides which checks count toward passing;
    the rest are still reported, as information."""
    only = case.get("only_checks")
    out = {}
    for name, fn in CHECKS.items():
        result = fn(case, run)
        if result is not None:
            out[name] = {"passed": result[0], "detail": result[1],
                         "counted": only is None or name in only}
    return out


def run_passed(checks: dict[str, dict]) -> bool:
    return all(c["passed"] for c in checks.values() if c.get("counted", True))
