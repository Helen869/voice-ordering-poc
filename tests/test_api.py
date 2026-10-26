"""API-level tests via FastAPI TestClient."""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


def test_get_menu_200(client):
    resp = client.get("/tools/menu")
    assert resp.status_code == 200
    body = resp.json()
    assert body["restaurant"] == "Golden Wok"
    assert body["currency"] == "USD"
    assert body["item_count"] == 19
    assert len(body["items"]) == 19


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "restaurant": "Golden Wok"}


def test_search_200_and_400_on_missing_q(client):
    resp = client.get("/tools/menu/search", params={"q": "chicken"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["match_count"] >= 3
    assert body["query"] == "chicken"

    resp = client.get("/tools/menu/search")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "missing_query"

    resp = client.get("/tools/menu/search", params={"q": "   "})
    assert resp.status_code == 400


def test_end_to_end_add_get_submit(client):
    session = "e2e-session-1"

    resp = client.post(
        "/tools/order/update",
        json={"session_id": session, "action": "add",
              "item_id": "egg-rolls", "quantity": 2},
    )
    assert resp.status_code == 200
    assert resp.json()["subtotal"] == 3.5

    resp = client.get(f"/tools/order/{session}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "draft"
    assert body["item_count"] == 2

    resp = client.post("/tools/order/submit", json={"session_id": session})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "submitted"
    assert re.fullmatch(r"GW-\d{4,}", body["confirmation_number"] or "")


def test_error_envelope_shape(client):
    resp = client.post(
        "/tools/order/update",
        json={"session_id": "s-err", "action": "add", "item_id": "cheeseburger"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) >= {"code", "message", "details"}
    assert body["error"]["code"] == "item_not_found"


def test_missing_session_id_returns_422_envelope(client):
    resp = client.post(
        "/tools/order/update",
        json={"action": "add", "item_id": "egg-rolls"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "invalid_request"


def test_submit_empty_order_422(client):
    resp = client.post("/tools/order/submit", json={"session_id": "never-used"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "empty_order"


def test_submit_twice_idempotent_flag(client):
    session = "e2e-session-2"
    client.post("/tools/order/update",
                json={"session_id": session, "action": "add", "item_id": "coke"})
    first = client.post("/tools/order/submit", json={"session_id": session}).json()
    second = client.post("/tools/order/submit", json={"session_id": session}).json()
    assert second["already_submitted"] is True
    assert second["confirmation_number"] == first["confirmation_number"]
