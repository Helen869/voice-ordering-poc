"""Unit tests for the eval checks (evals/checks.py) and cases file. No API calls."""

import asyncio
from pathlib import Path

import pytest

from evals.checks import (
    check_expected_calls,
    check_final_order,
    check_forbidden_calls,
    check_money_from_tools,
    check_no_leaks,
    check_no_premature_submit,
    check_required_modifiers_from_caller,
    check_speech,
    evaluate,
    load_menu,
    spoken_amounts,
)

MENU = load_menu()


def step(say="", agent="", calls=(), explicit_yes=False):
    return {"say": say, "agent": agent, "calls": list(calls), "explicit_yes": explicit_yes}


def call(name, args=None, result=None):
    return {"name": name, "args": args or {}, "result": result or {}}


def run_of(*steps, final=None, error=None):
    return {"steps": list(steps), "final_order": final or {}, "error": error}


# -- spoken amounts -------------------------------------------------------------
@pytest.mark.parametrize("text,amounts", [
    ("That's $12.49. Anything else?", [12.49]),
    ("one shrimp fried rice for 12.49", [12.49]),
    ("for eleven forty nine. Anything else?", [11.49]),
    ("is eleven dollars and forty-nine cents.", [11.49]),
    ("New total is twenty-six ninety-eight", [26.98]),
    ("thirty five forty six", [35.46]),
    ("just one Coke, two ninety-nine", [2.99]),
    ("large adds three dollars", [3.0]),
    ("two egg rolls and one chicken lo mein. Anything else?", []),
    ("Two Cokes. One large spicy General Tso's.", []),
    ("Your confirmation number is GW ten forty two. Thanks!", []),
    ("Order placed! Your confirmation number is 1042. That was $2.99.", [2.99]),
])
def test_spoken_amounts(text, amounts):
    assert spoken_amounts(text) == amounts


# -- money_from_tools -----------------------------------------------------------
def test_money_must_come_from_an_earlier_tool_result():
    ok = run_of(step(calls=[call("update_order", result={"subtotal": 12.49, "items": [
        {"unit_price": 12.49, "line_total": 12.49}]})], agent="That's twelve forty-nine."))
    assert check_money_from_tools({}, ok)[0]

    invented = run_of(step(agent="That's $13.00."), step(calls=[call("get_order", result={"subtotal": 13.0})]))
    passed, detail = check_money_from_tools({}, invented)
    assert not passed and "$13.00" in detail


SEARCH_FRIED_RICE = {"matches": [{"id": "fried-rice", "base_price": 9.99, "modifiers": [
    {"name": "protein", "options": [{"name": "vegetable", "price_delta": 0.0},
                                    {"name": "shrimp", "price_delta": 2.5}]}]}]}


def test_base_price_and_modifier_prices_are_allowed_verbatim():
    r = run_of(step(calls=[call("search_menu", result=SEARCH_FRIED_RICE)],
                    agent="Fried rice is $9.99, and shrimp adds two dollars and fifty cents."))
    passed, detail = check_money_from_tools({}, r)
    assert passed and detail == "2 amount(s) verified"


def test_computed_sum_not_in_any_result_fails():
    # 9.99 + 2.50 = 12.49 is correct arithmetic, but no tool result contains it.
    r = run_of(step(calls=[call("search_menu", result=SEARCH_FRIED_RICE)],
                    agent="Shrimp fried rice would be twelve forty-nine."))
    passed, detail = check_money_from_tools({}, r)
    assert not passed and "$12.49" in detail


# -- submit / required modifiers --------------------------------------------------
def test_premature_submit_detected():
    case = {"steps": [{"say": "That's all."}, {"say": "Yes", "explicit_yes": True}]}
    assert not check_no_premature_submit(case, run_of(step(calls=[call("submit_order")]), step()))[0]
    assert check_no_premature_submit(case, run_of(step(), step(calls=[call("submit_order")])))[0]
    assert not check_no_premature_submit({"steps": [{"say": "x"}]}, run_of(step(calls=[call("submit_order")])))[0]


def test_required_modifier_must_be_said_by_caller():
    guessed = run_of(step(say="I want fried rice.", calls=[call("update_order", {
        "action": "add", "item_id": "fried-rice", "modifiers": [{"name": "protein", "value": "chicken"}]})]))
    passed, detail = check_required_modifiers_from_caller({}, guessed, MENU)
    assert not passed and "protein=chicken" in detail

    asked = run_of(step(say="I want fried rice."), step(say="Shrimp.", calls=[call("update_order", {
        "action": "add", "item_id": "fried-rice", "modifiers": [{"name": "protein", "value": "shrimp"}]})]))
    assert check_required_modifiers_from_caller({}, asked, MENU)[0]


def test_optional_modifiers_and_no_modifier_items_are_not_checked():
    r = run_of(step(say="two cokes", calls=[call("update_order", {
        "action": "add", "item_id": "coke", "modifiers": [{"name": "size", "value": "large"}]})]))
    assert check_required_modifiers_from_caller({}, r, MENU) is None


# -- expected / forbidden calls, speech, leaks ------------------------------------------
def test_expected_calls_any_order_and_subset_args():
    case = {"expect_calls": [
        {"name": "update_order", "args": {"item_id": "lo-mein", "modifiers": {"protein": "beef"}}},
        {"name": "search_menu", "args": {"q": "cheeseburger"}},
    ]}
    r = run_of(step(calls=[
        call("search_menu", {"q": "Cheeseburger please"}),
        call("update_order", {"action": "add", "item_id": "lo-mein",
                              "modifiers": [{"name": "protein", "value": "beef"}]}),
    ]))
    assert check_expected_calls(case, r)[0]
    passed, detail = check_expected_calls(case, run_of(step(calls=[call("search_menu", {"q": "cheeseburger"})])))
    assert not passed and "lo-mein" in detail


def test_forbidden_calls_are_per_step():
    case = {"steps": [{"say": "a", "forbid_calls": ["update_order"]}, {"say": "b"}]}
    assert not check_forbidden_calls(case, run_of(step(calls=[call("update_order")]), step()))[0]
    assert check_forbidden_calls(case, run_of(step(), step(calls=[call("update_order")])))[0]


def test_speech_rules():
    case = {"steps": [{"say": "x", "say_all": ["vegetable", "shrimp"], "say_none": ["how many"]}]}
    assert check_speech(case, run_of(step(agent="Vegetable, chicken, beef, or shrimp?")))[0]
    assert not check_speech(case, run_of(step(agent="Vegetable? How many?")))[0]


def test_leaks():
    assert check_no_leaks({}, run_of(step(agent="Sorry, we don't have that.")))[0]
    for bad in ("backend unavailable", "System note: the order", "error item_not_found", '{"error"}'):
        assert not check_no_leaks({}, run_of(step(agent=bad)))[0]


# -- final order -----------------------------------------------------------------
def test_final_order_and_spoken_confirmation():
    case = {"final": {"state": "submitted", "subtotal": 2.99, "items": [{"item_id": "coke", "quantity": 1}]}}
    final = {"state": "submitted", "subtotal": 2.99, "confirmation_number": "GW-1042",
             "items": [{"item_id": "coke", "quantity": 1, "modifiers": {"size": "regular"}}]}
    from evals.checks import check_confirmation_spoken

    assert check_final_order(case, run_of(step(agent="Order placed!"), final=final))[0]
    assert check_confirmation_spoken(case, run_of(
        step(agent="Your confirmation number is G W ten forty-two."), final=final))[0]
    assert check_confirmation_spoken(case, run_of(step(agent="Confirmation GW-1042."), final=final))[0]
    passed, detail = check_confirmation_spoken(case, run_of(step(agent="Order placed!"), final=final))
    assert not passed and "never spoken" in detail
    assert not check_final_order(case, run_of(step(), final={**final, "confirmation_number": None}))[0]


def test_final_order_rejects_extra_items():
    case = {"final": {"state": "draft", "items": []}}
    final = {"state": "draft", "items": [{"item_id": "coke", "quantity": 1, "modifiers": {}}]}
    passed, detail = check_final_order(case, run_of(step(), final=final))
    assert not passed and "unexpected coke" in detail


def test_evaluate_marks_errors_as_failed_runs():
    checks = evaluate({"steps": [{"say": "hi"}]}, run_of(step(), error="timeout at step 1"))
    assert checks["completed"] == {"passed": False, "detail": "timeout at step 1", "counted": True,
                                   "warning": False}


# -- cases.yaml stays consistent with the menu ----------------------------------------
def _cases():
    yaml = pytest.importorskip("yaml")
    path = Path(__file__).resolve().parent.parent / "evals" / "cases.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]


def _price(item_id, modifiers):
    item = MENU[item_id]
    total = item["base_price"]
    for group in item["modifiers"]:
        chosen = modifiers.get(group["name"], group.get("default"))
        total += next((o["price_delta"] for o in group["options"] if o["name"] == chosen), 0)
    return total


def test_cases_file_is_valid_and_matches_menu():
    cases = _cases()
    assert len({c["id"] for c in cases}) == len(cases) == 11
    for case in cases:
        assert case["steps"] and all(s.get("say") for s in case["steps"]), case["id"]
        for exp in case.get("expect_calls", []):
            item_id = exp.get("args", {}).get("item_id")
            assert item_id is None or item_id in MENU, (case["id"], item_id)
        final = case.get("final")
        if final and final.get("items") is not None and "subtotal" in final:
            for it in final["items"]:
                groups = {g["name"]: {o["name"] for o in g["options"]} for g in MENU[it["item_id"]]["modifiers"]}
                for name, value in it.get("modifiers", {}).items():
                    assert value in groups[name], (case["id"], name, value)
            expected = round(sum(_price(i["item_id"], i.get("modifiers", {})) * i["quantity"]
                                 for i in final["items"]), 2)
            assert expected == final["subtotal"], (case["id"], expected, final["subtotal"])


# -- turn waiting (driver) ------------------------------------------------------------
def test_wait_turn_done_waits_for_settle_and_follow_up_turns():
    pytest.importorskip("yaml")
    from evals.run import _wait_turn_done

    class Bridge:
        tools_idle = True

    async def go():
        events = asyncio.Queue()
        for e in ("activity", "turn_complete", "turn_complete"):
            events.put_nowait(e)

        async def late():  # a follow-up turn (e.g. after a System note)
            await asyncio.sleep(0.05)
            events.put_nowait("activity")
            events.put_nowait("turn_complete")

        task = asyncio.create_task(late())
        await _wait_turn_done(events, Bridge(), timeout=2, settle=0.2)
        await task
        return events.qsize()

    assert asyncio.run(go()) == 0  # consumed the late turn before returning


def test_wait_turn_done_times_out():
    pytest.importorskip("yaml")
    from evals.run import _wait_turn_done

    class Bridge:
        tools_idle = True

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_wait_turn_done(asyncio.Queue(), Bridge(), timeout=0.1, settle=0.05))


# -- confirmation numbers and submit-once -----------------------------------------------
from evals.checks import check_confirmation_numbers, check_submit_at_most_once

SUBMIT_OK = {"state": "submitted", "confirmation_number": "GW-1045"}


def ev_run(*events, steps=None):
    seq = [dict(e, seq=i) for i, e in enumerate(events)]
    return {"events": seq, "steps": steps or [step()], "final_order": {}, "error": None}


def said(text):
    return {"type": "agent_text", "text": text}


def returned(result=SUBMIT_OK):
    return {"type": "tool_result", "name": "submit_order", "result": result}


def test_confirmation_spoken_after_result_passes():
    r = ev_run(returned(), said("Order placed! Your confirmation "), said("number is GW-10"), said("45."))
    assert check_confirmation_numbers({}, r) == (True, "ok: GW-1045")


def test_wrong_confirmation_number_fails():
    r = ev_run(returned(), said("Your confirmation number is GW-1042."), said(" Sorry, GW-1045."))
    passed, detail = check_confirmation_numbers({}, r)
    assert not passed and "spoke GW-1042, but submit_order returned GW-1045" in detail


def test_confirmation_spoken_before_result_fails():
    r = ev_run(said("Your confirmation number is GW-1045."), returned())
    passed, detail = check_confirmation_numbers({}, r)
    assert not passed and "before submit_order returned it" in detail


def test_number_spoken_but_never_returned_fails():
    r = ev_run(said("Your confirmation number is GW-1042."))
    passed, detail = check_confirmation_numbers({}, r)
    assert not passed and "returned none" in detail


def test_confirmation_check_not_applicable_without_numbers():
    assert check_confirmation_numbers({}, ev_run(said("Two Cokes, $5.98."))) is None
    assert check_confirmation_numbers({}, {"steps": [], "final_order": {}}) is None  # old reports


def test_submit_at_most_once():
    once = run_of(step(calls=[call("get_order"), call("submit_order", result=SUBMIT_OK)]))
    assert check_submit_at_most_once({}, once) == (True, "ok")
    twice = run_of(step(calls=[call("submit_order", result=SUBMIT_OK)]),
                   step(calls=[call("submit_order", result={**SUBMIT_OK, "already_submitted": True})]))
    passed, detail = check_submit_at_most_once({}, twice)
    assert passed and detail.startswith("warning: GW-1045 submitted 2 times")
    no_flag = run_of(step(calls=[call("submit_order", result=SUBMIT_OK), call("submit_order", result=SUBMIT_OK)]))
    assert not check_submit_at_most_once({}, no_flag)[0]  # a repeat NOT answered as already_submitted
    refused = run_of(step(calls=[call("submit_order", result={"error": {"code": "invalid_arguments"}}),
                                 call("submit_order", result=SUBMIT_OK)]))
    assert check_submit_at_most_once({}, refused)[0]  # a refused attempt is not a submit


def test_longer_made_up_number_is_reported_whole():
    r = ev_run(returned(), said("Your confirmation number is GW-12345."))
    passed, detail = check_confirmation_numbers({}, r)
    assert not passed and "spoke GW-12345" in detail


# -- wrong numbers corrected, get_order before submit, only_checks ----------------------------
from evals.checks import check_get_order_before_submit, check_wrong_number_corrected, run_passed


def caller(text):
    return {"type": "caller", "text": text}


def test_wrong_number_corrected_in_same_agent_turn():
    r = ev_run(caller("yes"), returned(), said("Your confirmation number is GW-1042. "),
               said("Sorry, the confirmation number is GW-1045."))
    assert check_wrong_number_corrected({}, r) == (True, "corrected: GW-1042")


def test_wrong_number_corrected_only_after_next_caller_turn_fails():
    r = ev_run(caller("yes"), returned(), said("Your confirmation number is GW-1042."),
               caller("ok"), said("Correction: GW-1045."))
    passed, detail = check_wrong_number_corrected({}, r)
    assert not passed and "GW-1042" in detail


def test_early_number_must_be_repeated_after_the_result():
    early_then_ok = ev_run(caller("yes"), said("Your number is GW-1045. "), returned(),
                           said("Your confirmation number is GW-1045."))
    assert check_wrong_number_corrected({}, early_then_ok)[0]
    early_only = ev_run(caller("yes"), said("Your number is GW-1045."), returned())
    assert not check_wrong_number_corrected({}, early_only)[0]


def test_wrong_number_check_not_applicable_when_all_correct():
    assert check_wrong_number_corrected({}, ev_run(caller("yes"), returned(), said("GW-1045."))) is None


def test_get_order_before_submit():
    good = run_of(step(calls=[call("update_order"), call("get_order"), call("submit_order", result=SUBMIT_OK)]))
    assert check_get_order_before_submit({}, good) == (True, "ok")
    stale = run_of(step(calls=[call("get_order"), call("update_order"), call("submit_order", result=SUBMIT_OK)]))
    assert not check_get_order_before_submit({}, stale)[0]
    refused_then_ok = run_of(step(calls=[
        call("update_order"),
        call("submit_order", result={"error": {"code": "invalid_arguments", "message": "x"}}),
        call("get_order"), call("submit_order", result=SUBMIT_OK)]))
    assert check_get_order_before_submit({}, refused_then_ok) == (True, "ok (1 attempt(s) refused by the bridge)")


def test_only_checks_decide_pass_but_others_are_still_reported():
    case = {"steps": [{"say": "x"}], "only_checks": ["completed"]}
    checks = evaluate(case, run_of(step(agent="backend unavailable")))
    assert checks["no_leaks"] == {"passed": False, "detail": "spoke: backend_unavailable", "counted": False,
                                  "warning": False}
    assert run_passed(checks)


def test_cases_only_checks_name_real_checks():
    from evals.checks import CHECKS

    for case in _cases():
        assert set(case.get("only_checks", [])) <= set(CHECKS), case["id"]


def test_asks_to_place_order():
    pytest.importorskip("yaml")
    from evals.run import asks_to_place_order

    assert asks_to_place_order("To confirm: one Coke, $2.99. Would you like me to place the order?")
    assert asks_to_place_order("Shall I go ahead and place your order?")
    assert not asks_to_place_order("Order placed! Your confirmation number is GW-1046.")
    assert not asks_to_place_order("Anything else?")


def test_leak_check_catches_markdown_and_markup():
    for bad in ("fried rice?```json\n{}\n```", "rice?\n---\n---", "Anything else?<no speech>"):
        assert not check_no_leaks({}, run_of(step(agent=bad)))[0], bad
    assert check_no_leaks({}, run_of(step(agent="Two Cokes - $5.98. Regular or large?")))[0]


def test_duplicate_submit_is_reported_as_warning():
    twice = run_of(step(calls=[call("get_order"), call("submit_order", result=SUBMIT_OK),
                               call("submit_order", result={**SUBMIT_OK, "already_submitted": True})]))
    checks = evaluate({"steps": [{"say": "x"}]}, twice)
    assert checks["submit_at_most_once"]["passed"] and checks["submit_at_most_once"]["warning"]
    assert run_passed(checks) or not all(c["passed"] for k, c in checks.items() if k != "submit_at_most_once")


# -- eval API key selection ------------------------------------------------------
def test_eval_key_preferred_then_main_key():
    pytest.importorskip("yaml")
    from evals.run import key_suffix, select_api_key

    both = {"GEMINI_EVAL_API_KEY": "eval-key-AAAA", "GEMINI_API_KEY": "main-key-BBBB"}
    assert select_api_key(both) == ("GEMINI_EVAL_API_KEY", "eval-key-AAAA")
    assert select_api_key({"GEMINI_EVAL_API_KEY": "  ", "GEMINI_API_KEY": "main-key-BBBB"}) == \
        ("GEMINI_API_KEY", "main-key-BBBB")  # empty value in .env counts as unset
    assert select_api_key({}) is None
    assert key_suffix("eval-key-AAAA") == "...AAAA"
    assert "AAAA" not in key_suffix("AAAA")  # never echo a whole short key


def test_concurrency_must_be_positive():
    pytest.importorskip("yaml")
    from evals.run import parse_args

    assert parse_args(["--concurrency", "1"]).concurrency == 1
    assert parse_args([]).concurrency == 3
    with pytest.raises(SystemExit):
        parse_args(["--concurrency", "0"])
