"""FastAPI entry point for the Golden Wok voice-ordering POC.

Routes are intentionally thin: parse the request, call a service, return the
response. All business logic (menu authority, order validation, pricing)
lives in app/menu_service.py and app/order_service.py.
"""

import json
import logging
import os
import time
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.menu_service import MenuService
from app.models import (
    OrderSubmitRequest,
    OrderUpdateRequest,
)
from app.order_service import OrderService, OrderServiceError
from app.session_store import InMemorySessionStore

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("voice_ordering")

menu_service = MenuService()
order_service = OrderService(menu_service, InMemorySessionStore())

app = FastAPI(title="Golden Wok Voice Ordering POC", version="0.1.0")


def log_tool_call(
    tool: str,
    session_id: Optional[str],
    action: Optional[str],
    success: bool,
    latency_ms: float,
) -> None:
    """One structured log line per tool call. Never logs order payloads or PII."""
    logger.info(
        json.dumps(
            {
                "event": "tool_call",
                "tool": tool,
                "session_id": session_id,
                "action": action,
                "success": success,
                "latency_ms": round(latency_ms, 2),
            }
        )
    )


def error_envelope(status: int, code: str, message: str, details: Optional[dict] = None):
    return JSONResponse(
        status_code=status,
        content={
            "error": {"code": code, "message": message, "details": details or {}}
        },
    )


@app.exception_handler(OrderServiceError)
async def order_service_error_handler(request: Request, exc: OrderServiceError):
    return error_envelope(exc.http_status, exc.code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Pydantic request errors (e.g. missing session_id) -> 422 error envelope.
    # Sanitize errors: raw error dicts can contain non-JSON-serializable values.
    safe_errors = [
        {
            "type": err.get("type"),
            "loc": [str(part) for part in err.get("loc", ())],
            "msg": err.get("msg"),
            "input": str(err.get("input")),
        }
        for err in exc.errors()
    ]
    return error_envelope(
        422,
        "invalid_request",
        "The request body is malformed or missing required fields.",
        {"validation_errors": safe_errors},
    )


@app.get("/health")
def health():
    return {"status": "ok", "restaurant": menu_service.restaurant}


@app.get("/tools/menu")
def get_menu():
    start = time.perf_counter()
    try:
        items = menu_service.get_menu()
        log_tool_call("get_menu", None, None, True, (time.perf_counter() - start) * 1000)
        return {
            "restaurant": menu_service.restaurant,
            "currency": menu_service.currency,
            "item_count": len(items),
            "items": [item.model_dump() for item in items],
        }
    except Exception:  # pragma: no cover - defensive
        log_tool_call("get_menu", None, None, False, (time.perf_counter() - start) * 1000)
        raise


@app.get("/tools/menu/search")
def search_menu(q: Optional[str] = Query(default=None)):
    start = time.perf_counter()
    if q is None or not q.strip():
        log_tool_call("search_menu", None, None, False, (time.perf_counter() - start) * 1000)
        return error_envelope(
            400,
            "missing_query",
            "Query parameter 'q' is required and must not be blank.",
        )
    matches = menu_service.search_menu(q)
    log_tool_call("search_menu", None, None, True, (time.perf_counter() - start) * 1000)
    return {
        "query": q,
        "match_count": len(matches),
        "matches": [item.model_dump() for item in matches],
    }


@app.post("/tools/order/update")
def update_order(req: OrderUpdateRequest):
    start = time.perf_counter()
    try:
        if req.action == "add":
            summary = order_service.add(
                req.session_id, req.item_id, req.quantity, req.modifiers
            )
        elif req.action == "remove":
            summary = order_service.remove(
                req.session_id, req.item_id, req.quantity, req.modifiers
            )
        else:  # update
            summary = order_service.update(
                req.session_id, req.item_id, req.quantity, req.modifiers, req.set_modifiers
            )
        log_tool_call(
            "update_order", req.session_id, req.action, True,
            (time.perf_counter() - start) * 1000,
        )
        return summary.model_dump()
    except Exception:
        log_tool_call(
            "update_order", req.session_id, req.action, False,
            (time.perf_counter() - start) * 1000,
        )
        raise


@app.get("/tools/order/{session_id}")
def get_order(session_id: str):
    start = time.perf_counter()
    try:
        summary = order_service.get_order(session_id)
        log_tool_call("get_order", session_id, None, True, (time.perf_counter() - start) * 1000)
        return summary.model_dump()
    except Exception:
        log_tool_call("get_order", session_id, None, False, (time.perf_counter() - start) * 1000)
        raise


@app.post("/tools/order/submit")
def submit_order(req: OrderSubmitRequest):
    start = time.perf_counter()
    try:
        summary, already_submitted = order_service.submit(req.session_id)
        payload = summary.model_dump()
        if already_submitted:
            payload["already_submitted"] = True
        log_tool_call(
            "submit_order", req.session_id, "submit", True,
            (time.perf_counter() - start) * 1000,
        )
        return payload
    except Exception:
        log_tool_call(
            "submit_order", req.session_id, "submit", False,
            (time.perf_counter() - start) * 1000,
        )
        raise
