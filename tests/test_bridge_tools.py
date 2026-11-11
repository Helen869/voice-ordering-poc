"""Unit tests for the Live bridge function-call router (live/bridge/tools.py)."""

import asyncio

import httpx
import pytest

from app.main import app
from live.bridge.tools import (
    BACKEND_UNAVAILABLE,
    INVALID_ARGUMENTS,
    BackendResponse,
    BackendUnavailable,
    HttpBackend,
    ToolRouter,
)

SID = "live-test-session"


def run(coro):
    return asyncio.run(coro)


def summary(state="draft", **extra):
    return {
        "session_id": SID,
        "state": state,
        "currency": "USD",
        "items": [{"item_id": "coke", "quantity": 1, "session_id": SID}],
        "item_count": 1,
        "subtotal": 2.99,
        **extra,
    }


class FakeBackend:
    """Records requests; answers from a {(method, path): response-or-exception} map."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.requests = []

    async def request(self, method, path, *, params=None, json=None):
        self.requests.append({"method": method, "path": path, "params": params, "json": json})
        answer = self.routes.get((method, path), BackendResponse(200, summary()))
        if isinstance(answer, Exception):
            raise answer
        return answer


def router(backend):
    return ToolRouter(backend, SID)


# -- session_id injection ----------------------------------------------------
def test_update_order_injects_session_id():
    be = FakeBackend()
    run(router(be).call("update_order", {"action": "add", "item_id": "coke"}))
    assert be.requests == [
        {"method": "POST", "path": "/tools/order/update", "params": None,
         "json": {"action": "add", "item_id": "coke", "session_id": SID}}
    ]


def test_get_order_injects_session_id_in_path():
    be = FakeBackend()
    run(router(be).call("get_order", {}))
    assert be.requests[0]["method"] == "GET"
    assert be.requests[0]["path"] == f"/tools/order/{SID}"


def test_submit_order_injects_session_id():
    be = FakeBackend({("POST", "/tools/order/submit"): BackendResponse(
        200, summary("submitted", confirmation_number="GW-1042"))})
    run(router(be).call("submit_order", None))
    assert be.requests[0]["json"] == {"session_id": SID}


@pytest.mark.parametrize("name,args", [
    ("update_order", {"action": "add", "item_id": "coke", "session_id": "other"}),
    ("get_order", {"session_id": "other"}),
    ("submit_order", {"session_id": "other"}),
])
def test_model_supplied_session_id_is_rejected(name, args):
    be = FakeBackend()
    result = run(router(be).call(name, args))
    assert result["error"]["code"] == INVALID_ARGUMENTS
    assert "session_id" in result["error"]["message"]
    assert be.requests == []


def test_session_id_is_url_quoted_in_get_order_path():
    be = FakeBackend()
    run(ToolRouter(be, "a/b c").call("get_order", {}))
    assert be.requests[0]["path"] == "/tools/order/a%2Fb%20c"


# -- session_id stripped from responses ------------------------------------
@pytest.mark.parametrize("name,args", [
    ("update_order", {"action": "add", "item_id": "coke"}),
    ("get_order", {}),
    ("submit_order", {}),
])
def test_session_id_stripped_from_responses(name, args):
    be = FakeBackend({("POST", "/tools/order/submit"): BackendResponse(
        200, summary("submitted", confirmation_number="GW-1042"))})
    result = run(router(be).call(name, args))
    assert "session_id" not in result
    assert all("session_id" not in item for item in result["items"])
    assert result["subtotal"] == 2.99


def test_session_id_stripped_from_error_details():
    body = {"error": {"code": "line_not_found", "message": "x", "details": {"session_id": SID}}}
    be = FakeBackend({("POST", "/tools/order/update"): BackendResponse(404, body)})
    result = run(router(be).call("update_order", {"action": "remove", "item_id": "coke"}))
    assert result == {"error": {"code": "line_not_found", "message": "x", "details": {}}}


# -- array -> dict conversion -------------------------------------------------
def test_modifier_arrays_converted_to_dicts():
    be = FakeBackend()
    run(router(be).call("update_order", {
        "action": "update", "item_id": "general-tsos-chicken",
        "set_modifiers": [{"name": "size", "value": "large"},
                          {"name": "spice_level", "value": "spicy"}],
    }))
    assert be.requests[0]["json"]["set_modifiers"] == {"size": "large", "spice_level": "spicy"}

    run(router(be).call("update_order", {
        "action": "add", "item_id": "fried-rice",
        "modifiers": [{"name": "protein", "value": "shrimp"}],
    }))
    assert be.requests[1]["json"]["modifiers"] == {"protein": "shrimp"}


def test_empty_modifier_array_is_omitted():
    be = FakeBackend()
    run(router(be).call("update_order", {"action": "add", "item_id": "lo-mein", "modifiers": []}))
    assert "modifiers" not in be.requests[0]["json"]


def test_integral_float_quantity_accepted_as_int():
    be = FakeBackend()
    run(router(be).call("update_order", {"action": "add", "item_id": "coke", "quantity": 2.0}))
    assert be.requests[0]["json"]["quantity"] == 2
    assert isinstance(be.requests[0]["json"]["quantity"], int)


# -- invalid_arguments ---------------------------------------------------------
def test_duplicate_modifier_name_rejected():
    be = FakeBackend()
    result = run(router(be).call("update_order", {
        "action": "add", "item_id": "general-tsos-chicken",
        "modifiers": [{"name": "size", "value": "regular"}, {"name": "size", "value": "large"}],
    }))
    assert result["error"]["code"] == INVALID_ARGUMENTS
    assert "'size' more than once" in result["error"]["message"]
    assert be.requests == []


@pytest.mark.parametrize("args,fragment", [
    ({"action": "add", "item_id": "coke", "colour": "red"}, "undeclared argument(s) colour"),
    ({"action": "add", "item_id": 7}, "'item_id' must be a string"),
    ({"action": "add", "item_id": "coke", "quantity": 1.5}, "'quantity' must be a whole number"),
    ({"action": "add", "item_id": "coke", "quantity": True}, "'quantity' must be a whole number"),
    ({"action": "replace", "item_id": "coke"}, "'action' must be one of: add, remove, update"),
    ({"item_id": "coke"}, "missing required argument(s) action"),
    ({"action": "add", "item_id": "coke", "modifiers": {"size": "large"}}, "'modifiers' must be a list"),
    ({"action": "add", "item_id": "coke", "modifiers": [{"name": "size"}]}, "missing required argument(s) value"),
])
def test_bad_arguments_rejected(args, fragment):
    be = FakeBackend()
    result = run(router(be).call("update_order", args))
    assert result["error"]["code"] == INVALID_ARGUMENTS
    assert fragment in result["error"]["message"]
    assert be.requests == []


def test_unknown_function_rejected():
    result = run(router(FakeBackend()).call("place_order", {}))
    assert result["error"]["code"] == INVALID_ARGUMENTS


def test_backend_request_errors_become_invalid_arguments():
    body = {"error": {"code": "missing_query", "message": "Query parameter 'q' is required."}}
    be = FakeBackend({("GET", "/tools/menu/search"): BackendResponse(400, body)})
    result = run(router(be).call("search_menu", {"q": " "}))
    assert result["error"]["code"] == INVALID_ARGUMENTS


# -- backend_unavailable --------------------------------------------------------
@pytest.mark.parametrize("answer", [
    BackendUnavailable("timeout"),
    BackendResponse(503, {"detail": "down"}),
    BackendResponse(200, None),  # non-JSON body
])
def test_unusable_backend_reply_is_backend_unavailable(answer):
    be = FakeBackend({("GET", "/tools/menu"): answer})
    result = run(router(be).call("get_menu", {}))
    assert result["error"]["code"] == BACKEND_UNAVAILABLE


# -- submit_order timeout / 5xx -> get_order check ---------------------------------
@pytest.mark.parametrize("failure", [BackendUnavailable("timeout"), BackendResponse(502, None)])
def test_submit_failure_but_order_submitted_returns_success(failure):
    be = FakeBackend({
        ("POST", "/tools/order/submit"): failure,
        ("GET", f"/tools/order/{SID}"): BackendResponse(
            200, summary("submitted", confirmation_number="GW-1042")),
    })
    result = run(router(be).call("submit_order", {}))
    assert [r["path"] for r in be.requests] == ["/tools/order/submit", f"/tools/order/{SID}"]
    assert result["state"] == "submitted"
    assert result["confirmation_number"] == "GW-1042"
    assert "session_id" not in result


@pytest.mark.parametrize("failure", [BackendUnavailable("timeout"), BackendResponse(500, None)])
def test_submit_failure_and_order_still_draft_is_backend_unavailable(failure):
    be = FakeBackend({
        ("POST", "/tools/order/submit"): failure,
        ("GET", f"/tools/order/{SID}"): BackendResponse(200, summary("draft")),
    })
    result = run(router(be).call("submit_order", {}))
    assert result["error"]["code"] == BACKEND_UNAVAILABLE
    assert len(be.requests) == 2


def test_submit_failure_and_get_order_failure_is_backend_unavailable():
    be = FakeBackend({
        ("POST", "/tools/order/submit"): BackendUnavailable("timeout"),
        ("GET", f"/tools/order/{SID}"): BackendUnavailable("timeout"),
    })
    result = run(router(be).call("submit_order", {}))
    assert result["error"]["code"] == BACKEND_UNAVAILABLE


def test_submit_business_error_is_not_retried():
    body = {"error": {"code": "empty_order", "message": "Cannot submit an empty order."}}
    be = FakeBackend({("POST", "/tools/order/submit"): BackendResponse(422, body)})
    result = run(router(be).call("submit_order", {}))
    assert result["error"]["code"] == "empty_order"
    assert len(be.requests) == 1


# -- HttpBackend ----------------------------------------------------------------
def test_http_backend_timeout_raises_backend_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async def go():
        be = HttpBackend("http://backend", transport=httpx.MockTransport(handler))
        try:
            return await ToolRouter(be, SID).call("get_menu", {})
        finally:
            await be.aclose()

    assert run(go())["error"]["code"] == BACKEND_UNAVAILABLE


def test_end_to_end_against_real_backend():
    """Router + HttpBackend + the real FastAPI app (in-process)."""
    async def go():
        be = HttpBackend("http://backend", transport=httpx.ASGITransport(app=app))
        r = ToolRouter(be, "live-e2e-" + str(id(be)))
        try:
            added = await r.call("update_order", {
                "action": "add", "item_id": "fried-rice",
                "modifiers": [{"name": "protein", "value": "shrimp"}],
            })
            order = await r.call("get_order", {})
            placed = await r.call("submit_order", {})
            return added, order, placed
        finally:
            await be.aclose()

    added, order, placed = run(go())
    assert added["subtotal"] == 12.49
    assert order["items"][0]["modifiers"] == {"protein": "shrimp"}
    assert placed["state"] == "submitted" and placed["confirmation_number"]
    for result in (added, order, placed):
        assert "session_id" not in result


# -- backend error mapping: explicit allowlist ------------------------------------
def _backend_error_codes():
    """Every code the backend's OrderServiceError subclasses can return."""
    import app.order_service as svc

    found, stack = set(), [svc.OrderServiceError]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            stack.append(sub)
            if isinstance(getattr(sub, "code", None), str):
                found.add((sub.code, sub.http_status))
    return sorted(found)


def test_argument_error_allowlist_is_exactly_three_codes():
    from live.bridge.tools import _BACKEND_ARGUMENT_ERRORS

    assert _BACKEND_ARGUMENT_ERRORS == {"invalid_request", "missing_query", "nothing_to_update"}


@pytest.mark.parametrize("code", ["invalid_request", "missing_query", "nothing_to_update"])
def test_allowlisted_backend_codes_become_invalid_arguments(code):
    body = {"error": {"code": code, "message": "bad request", "details": {}}}
    be = FakeBackend({("POST", "/tools/order/update"): BackendResponse(422, body)})
    result = run(router(be).call("update_order", {"action": "add", "item_id": "coke"}))
    assert result["error"]["code"] == INVALID_ARGUMENTS


def test_backend_error_codes_discovered():
    codes = {c for c, _ in _backend_error_codes()}
    assert {"item_not_found", "nothing_to_update", "order_locked", "empty_order"} <= codes


@pytest.mark.parametrize("code,status", [
    (c, s) for c, s in _backend_error_codes() if c != "nothing_to_update"
])
def test_other_backend_error_codes_pass_through_unchanged(code, status):
    body = {"error": {"code": code, "message": f"msg for {code}", "details": {"item_id": "coke"}}}
    be = FakeBackend({("POST", "/tools/order/update"): BackendResponse(status, body)})
    result = run(router(be).call("update_order", {"action": "update", "item_id": "coke"}))
    assert result == body


def test_nothing_to_update_from_real_backend_becomes_invalid_arguments():
    async def go():
        be = HttpBackend("http://backend", transport=httpx.ASGITransport(app=app))
        r = ToolRouter(be, "live-ntu-" + str(id(be)))
        try:
            await r.call("update_order", {"action": "add", "item_id": "coke"})
            return await r.call("update_order", {
                "action": "update", "item_id": "coke", "set_modifiers": [],
            })
        finally:
            await be.aclose()

    result = run(go())
    assert result["error"]["code"] == INVALID_ARGUMENTS
    assert "Fix and retry" in result["error"]["message"]


# -- submit_order guard: get_order must follow the last change ----------------------------
SUBMITTED = BackendResponse(200, summary("submitted", confirmation_number="GW-1042"))


def test_submit_refused_after_update_without_get_order():
    be = FakeBackend({("POST", "/tools/order/submit"): SUBMITTED})
    r = router(be)

    async def go():
        await r.call("update_order", {"action": "add", "item_id": "coke"})
        return await r.call("submit_order", {})

    result = run(go())
    assert result["error"]["code"] == INVALID_ARGUMENTS
    assert "call get_order and read the order back to the caller first" in result["error"]["message"].lower()
    assert [q["path"] for q in be.requests] == ["/tools/order/update"]  # submit never sent


def test_submit_allowed_after_get_order():
    be = FakeBackend({("POST", "/tools/order/submit"): SUBMITTED})
    r = router(be)

    async def go():
        await r.call("update_order", {"action": "add", "item_id": "coke"})
        await r.call("get_order", {})
        return await r.call("submit_order", {})

    assert run(go())["confirmation_number"] == "GW-1042"


def test_change_after_get_order_requires_another_get_order():
    be = FakeBackend({("POST", "/tools/order/submit"): SUBMITTED})
    r = router(be)

    async def go():
        await r.call("update_order", {"action": "add", "item_id": "coke"})
        await r.call("get_order", {})
        await r.call("update_order", {"action": "update", "item_id": "coke", "quantity": 2})
        blocked = await r.call("submit_order", {})
        await r.call("get_order", {})
        return blocked, await r.call("submit_order", {})

    blocked, placed = run(go())
    assert blocked["error"]["code"] == INVALID_ARGUMENTS
    assert placed["state"] == "submitted"


def test_failed_update_or_failed_get_order_do_not_change_the_guard():
    err = BackendResponse(404, {"error": {"code": "item_not_found", "message": "x"}})
    be = FakeBackend({("POST", "/tools/order/update"): err, ("POST", "/tools/order/submit"): SUBMITTED})
    r = router(be)
    # A rejected update changes nothing, so submit is not blocked by it.
    assert run(r.call("update_order", {"action": "add", "item_id": "pizza"}))["error"]["code"] == "item_not_found"
    assert run(r.call("submit_order", {}))["state"] == "submitted"

    be2 = FakeBackend({("GET", f"/tools/order/{SID}"): BackendUnavailable("down")})
    r2 = router(be2)

    async def go():
        await r2.call("update_order", {"action": "add", "item_id": "coke"})
        await r2.call("get_order", {})  # fails: the read-back did not happen
        return await r2.call("submit_order", {})

    assert run(go())["error"]["code"] == INVALID_ARGUMENTS
