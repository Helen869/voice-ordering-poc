"""Function-call router: Gemini Live function calls -> FastAPI backend.

The model never sees or sends a session id. For each call the router:

1. validates the arguments against ``live/function_declarations.json``
   (undeclared args, wrong types, duplicate modifier names -> ``invalid_arguments``),
2. converts ``[{name, value}, ...]`` modifier arrays back to dicts,
3. injects the per-call ``session_id`` into update_order / get_order / submit_order,
4. forwards to the backend and returns the JSON body with every ``session_id`` removed.

Timeouts, network failures, 5xx and non-JSON replies become ``backend_unavailable``.
submit_order is special: its outcome is unknown after a timeout/5xx, so the router
checks get_order and reports success if the order was in fact submitted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol
from urllib.parse import quote

DECLARATIONS_PATH = Path(__file__).resolve().parent.parent / "function_declarations.json"

INVALID_ARGUMENTS = "invalid_arguments"
BACKEND_UNAVAILABLE = "backend_unavailable"

# Backend codes meaning the request itself was malformed. Not in the spoken error
# table, so the model gets them as invalid_arguments and fixes the call silently.
_BACKEND_ARGUMENT_ERRORS = {"invalid_request", "missing_query"}

_MODIFIER_ARRAY_ARGS = ("modifiers", "set_modifiers")
_NO_PARAMETERS = {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# Backend transport
# ---------------------------------------------------------------------------
class BackendUnavailable(Exception):
    """The backend could not be reached or gave no usable answer."""


@dataclass
class BackendResponse:
    status: int
    body: Any  # parsed JSON, or None if the body was not JSON


class Backend(Protocol):
    async def request(
        self, method: str, path: str, *, params: Optional[dict] = None, json: Optional[dict] = None
    ) -> BackendResponse: ...


class HttpBackend:
    """httpx-based backend client. One instance per process, shared by all calls."""

    def __init__(self, base_url: str, *, timeout: float = 5.0, transport: Any = None) -> None:
        import httpx

        self._httpx = httpx
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def request(
        self, method: str, path: str, *, params: Optional[dict] = None, json: Optional[dict] = None
    ) -> BackendResponse:
        try:
            resp = await self._client.request(method, path, params=params, json=json)
        except self._httpx.HTTPError as exc:  # timeouts and connection errors
            raise BackendUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = None
        return BackendResponse(resp.status_code, body)

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Argument validation and conversion
# ---------------------------------------------------------------------------
class ArgumentError(Exception):
    pass


def load_declarations(path: Path = DECLARATIONS_PATH) -> dict[str, dict]:
    return {d["name"]: d for d in json.loads(path.read_text(encoding="utf-8"))}


def _join(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _validate(value: Any, schema: dict, where: str) -> Any:
    """Check ``value`` against the declaration subset we use; return it normalized."""
    kind = schema.get("type")
    label = where or "arguments"
    if kind == "string":
        if not isinstance(value, str):
            raise ArgumentError(f"'{label}' must be a string")
        if "enum" in schema and value not in schema["enum"]:
            raise ArgumentError(f"'{label}' must be one of: {', '.join(schema['enum'])}")
        return value
    if kind == "integer":
        # Function-call args travel as JSON numbers, so 2 may arrive as 2.0.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ArgumentError(f"'{label}' must be a whole number")
        if isinstance(value, float) and not value.is_integer():
            raise ArgumentError(f"'{label}' must be a whole number")
        return int(value)
    if kind == "array":
        if not isinstance(value, list):
            raise ArgumentError(f"'{label}' must be a list")
        return [_validate(v, schema["items"], f"{label}[{i}]") for i, v in enumerate(value)]
    if kind == "object":
        if not isinstance(value, dict):
            raise ArgumentError(f"'{label}' must be an object")
        props = schema.get("properties", {})
        extra = sorted(set(value) - set(props))
        if extra:
            allowed = ", ".join(props) or "none"
            raise ArgumentError(
                f"undeclared argument(s) {', '.join(extra)}"
                + (f" in '{where}'" if where else "")
                + f"; allowed: {allowed}"
            )
        missing = [k for k in schema.get("required", []) if k not in value]
        if missing:
            raise ArgumentError(
                f"missing required argument(s) {', '.join(missing)}"
                + (f" in '{where}'" if where else "")
            )
        return {k: _validate(v, props[k], _join(where, k)) for k, v in value.items()}
    return value


def _pairs_to_dict(pairs: list[dict], arg: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if pair["name"] in out:
            raise ArgumentError(
                f"'{arg}' lists modifier '{pair['name']}' more than once; send each name once"
            )
        out[pair["name"]] = pair["value"]
    return out


def strip_session_id(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: strip_session_id(v) for k, v in obj.items() if k != "session_id"}
    if isinstance(obj, list):
        return [strip_session_id(v) for v in obj]
    return obj


def error_result(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "details": {}}}


def _unavailable(message: str = "The ordering system could not be reached.") -> dict:
    return error_result(BACKEND_UNAVAILABLE, message)


def _is_unusable(resp: BackendResponse) -> bool:
    return resp.status >= 500 or not isinstance(resp.body, dict)


def is_successful_submit(result: dict) -> bool:
    return (
        "error" not in result
        and result.get("state") == "submitted"
        and bool(result.get("confirmation_number"))
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
class ToolRouter:
    """Executes the model's function calls for ONE phone call (one session_id)."""

    def __init__(
        self, backend: Backend, session_id: str, declarations: Optional[dict[str, dict]] = None
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        self._backend = backend
        self._session_id = session_id
        self._decls = declarations if declarations is not None else load_declarations()

    @property
    def session_id(self) -> str:
        return self._session_id

    async def call(self, name: str, args: Optional[dict]) -> dict:
        """Run one function call; always returns a dict for the model, never raises."""
        decl = self._decls.get(name)
        if decl is None:
            return error_result(
                INVALID_ARGUMENTS,
                f"Unknown function '{name}'. Available: {', '.join(self._decls)}.",
            )
        try:
            clean = _validate(args or {}, decl.get("parameters", _NO_PARAMETERS), "")
            for arg in _MODIFIER_ARRAY_ARGS:
                if arg in clean:
                    converted = _pairs_to_dict(clean.pop(arg), arg)
                    if converted:  # empty list == omitted (keeps modifier inheritance on add)
                        clean[arg] = converted
        except ArgumentError as exc:
            return error_result(
                INVALID_ARGUMENTS, f"{name}: {exc}. Fix the arguments and call {name} again."
            )

        try:
            resp = await getattr(self, f"_{name}")(clean)
        except BackendUnavailable:
            return _unavailable()
        return self._to_model(resp)

    # -- per-function handlers ---------------------------------------------
    async def _get_menu(self, args: dict) -> BackendResponse:
        return await self._backend.request("GET", "/tools/menu")

    async def _search_menu(self, args: dict) -> BackendResponse:
        return await self._backend.request("GET", "/tools/menu/search", params={"q": args["q"]})

    async def _update_order(self, args: dict) -> BackendResponse:
        return await self._backend.request(
            "POST", "/tools/order/update", json={**args, "session_id": self._session_id}
        )

    async def _get_order(self, args: dict) -> BackendResponse:
        return await self._backend.request(
            "GET", f"/tools/order/{quote(self._session_id, safe='')}"
        )

    async def _submit_order(self, args: dict) -> BackendResponse:
        try:
            resp = await self._backend.request(
                "POST", "/tools/order/submit", json={"session_id": self._session_id}
            )
            if not _is_unusable(resp):
                return resp
        except BackendUnavailable:
            pass
        # Outcome unknown: the submit may have been applied before the failure.
        check = await self._get_order({})
        if (
            check.status == 200
            and isinstance(check.body, dict)
            and check.body.get("state") == "submitted"
        ):
            return check
        raise BackendUnavailable("submit_order failed and the order is not submitted")

    # -- response shaping ---------------------------------------------------
    def _to_model(self, resp: BackendResponse) -> dict:
        if _is_unusable(resp):
            return _unavailable()
        body = strip_session_id(resp.body)
        if 200 <= resp.status < 300:
            return body
        err = body.get("error") if isinstance(body, dict) else None
        if not isinstance(err, dict) or "code" not in err:
            return _unavailable()
        if err["code"] in _BACKEND_ARGUMENT_ERRORS:
            return error_result(
                INVALID_ARGUMENTS, f"{err.get('message', 'Malformed request.')} Fix and retry."
            )
        return body
