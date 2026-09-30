"""Order business logic for the Golden Wok voice-ordering POC.

This module owns ALL business validation and pricing. Routes must stay thin:
parse the request, call OrderService, return the summary.

Design notes:
- Line identity = (item_id, normalized modifiers with defaults applied).
- Prices are computed in integer cents; floats are only exposed on the API.
- The session store is injected via the constructor so it can be replaced
  (e.g. Firestore/Redis) without touching any of this logic.
- Confirmation numbers come from a process-local counter starting at GW-1042.
  This is an intentional POC limitation: numbers are not durable across
  restarts and are not safe under multi-instance Cloud Run.
"""

import threading
from typing import Dict, List, Optional, Tuple

from app.menu_service import MenuService
from app.models import (
    OrderLineSummary,
    OrderSummary,
)
from app.session_store import SessionStore, StoredOrder, StoredOrderLine

MAX_QUANTITY_PER_LINE = 20

_FIRST_CONFIRMATION_NUMBER = 1042


# ---------------------------------------------------------------------------
# Service-level exceptions. Routes map these to the HTTP error envelope.
# ---------------------------------------------------------------------------
class OrderServiceError(Exception):
    """Base class. Carries the snake_case code used in the error envelope."""

    http_status: int = 400
    code: str = "order_error"

    def __init__(self, message: str, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class MenuItemNotFound(OrderServiceError):
    http_status = 404
    code = "item_not_found"


class ItemUnavailable(OrderServiceError):
    http_status = 409
    code = "item_unavailable"


class UnknownModifier(OrderServiceError):
    http_status = 422
    code = "unknown_modifier"


class InvalidModifierOption(OrderServiceError):
    http_status = 422
    code = "invalid_modifier_option"


class MissingRequiredModifiers(OrderServiceError):
    http_status = 422
    code = "missing_required_modifiers"


class InvalidQuantity(OrderServiceError):
    http_status = 422
    code = "invalid_quantity"


class LineNotFound(OrderServiceError):
    http_status = 404
    code = "line_not_found"


class AmbiguousItemReference(OrderServiceError):
    http_status = 409
    code = "ambiguous_item_reference"


class NothingToUpdate(OrderServiceError):
    http_status = 400
    code = "nothing_to_update"


class EmptyOrder(OrderServiceError):
    http_status = 422
    code = "empty_order"


class OrderLocked(OrderServiceError):
    """Raised when mutating an already-submitted order."""

    http_status = 409
    code = "order_locked"


# ---------------------------------------------------------------------------
# OrderService
# ---------------------------------------------------------------------------
class OrderService:
    _confirmation_counter = _FIRST_CONFIRMATION_NUMBER
    _confirmation_lock = threading.Lock()

    def __init__(self, menu_service: MenuService, store: SessionStore) -> None:
        self._menu = menu_service
        self._store = store

    # -- public API ---------------------------------------------------------
    def get_order(self, session_id: str) -> OrderSummary:
        """Return the order summary; an unknown session starts as empty draft."""
        return self._summarize(self._get_or_create(session_id))

    def add(
        self,
        session_id: str,
        item_id: str,
        quantity: Optional[int] = None,
        modifiers: Optional[Dict[str, str]] = None,
    ) -> OrderSummary:
        """Add an item to the draft order, merging with an identical line."""
        order = self._get_or_create(session_id)
        self._require_draft(order)
        item = self._resolve_item(item_id)

        qty = 1 if quantity is None else quantity
        self._validate_quantity(qty, allow_zero=False)

        effective = self._apply_inheritance(item, order, modifiers or {})
        resolved = self._resolve_modifiers(item, effective, check_required=True)

        existing = self._find_line(order, item.id, resolved)
        if existing is not None:
            new_qty = existing.quantity + qty
            if new_qty > MAX_QUANTITY_PER_LINE:
                raise InvalidQuantity(
                    f"Adding {qty} would exceed the per-line maximum of "
                    f"{MAX_QUANTITY_PER_LINE}.",
                    {"max_per_line": MAX_QUANTITY_PER_LINE},
                )
            existing.quantity = new_qty
        else:
            order.lines.append(
                StoredOrderLine(item_id=item.id, modifiers=resolved, quantity=qty)
            )
        self._store.save(session_id, order)
        return self._summarize(order)

    def remove(
        self,
        session_id: str,
        item_id: str,
        quantity: Optional[int] = None,
        modifiers: Optional[Dict[str, str]] = None,
    ) -> OrderSummary:
        """Remove quantity from a line, or the whole line when quantity is omitted."""
        order = self._get_or_create(session_id)
        self._require_draft(order)
        item = self._resolve_item(item_id)

        line = self._resolve_single_line(order, item.id, modifiers)
        if quantity is None:
            order.lines.remove(line)
        else:
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
                raise InvalidQuantity(
                    "Remove quantity must be a positive integer.",
                    {"received": quantity},
                )
            remaining = line.quantity - quantity
            if remaining <= 0:
                order.lines.remove(line)
            else:
                line.quantity = remaining
        self._store.save(session_id, order)
        return self._summarize(order)

    def update(
        self,
        session_id: str,
        item_id: str,
        quantity: Optional[int] = None,
        modifiers: Optional[Dict[str, str]] = None,
        set_modifiers: Optional[Dict[str, str]] = None,
    ) -> OrderSummary:
        """Set a line's quantity and/or override its modifiers."""
        order = self._get_or_create(session_id)
        self._require_draft(order)
        item = self._resolve_item(item_id)

        if quantity is None and not set_modifiers:
            raise NothingToUpdate(
                "Nothing to update: provide quantity and/or set_modifiers.",
                {"item_id": item.id},
            )

        line = self._resolve_single_line(order, item.id, modifiers)
        if set_modifiers:
            merged = {**line.modifiers, **set_modifiers}
            line.modifiers = self._resolve_modifiers(item, merged, check_required=True)
        if quantity is not None:
            self._validate_quantity(quantity, allow_zero=True)
            if quantity == 0:
                order.lines.remove(line)
            else:
                line.quantity = quantity
        self._store.save(session_id, order)
        return self._summarize(order)

    def submit(self, session_id: str) -> Tuple[OrderSummary, bool]:
        """Submit the draft order. Repeat submits are idempotent.

        Returns (summary, already_submitted).
        """
        order = self._get_or_create(session_id)
        if order.state == "submitted":
            summary = self._summarize(order)
            summary.already_submitted = True
            return summary, True
        if not order.lines:
            raise EmptyOrder(
                "Cannot submit an empty order. Add at least one item first.",
                {"session_id": session_id},
            )
        order.state = "submitted"
        order.confirmation_number = self._next_confirmation_number()
        self._store.save(session_id, order)
        return self._summarize(order), False

    # -- confirmation numbers -----------------------------------------------
    @classmethod
    def _next_confirmation_number(cls) -> str:
        """Process-local counter starting at GW-1042 (POC limitation)."""
        with cls._confirmation_lock:
            number = cls._confirmation_counter
            cls._confirmation_counter += 1
        return f"GW-{number}"

    # -- internal helpers ----------------------------------------------------
    def _get_or_create(self, session_id: str) -> StoredOrder:
        order = self._store.get(session_id)
        if order is None:
            order = StoredOrder(session_id=session_id)
            self._store.save(session_id, order)
        return order

    @staticmethod
    def _require_draft(order: StoredOrder) -> None:
        if order.state != "draft":
            raise OrderLocked(
                "This order has already been submitted and can no longer be changed.",
                {
                    "session_id": order.session_id,
                    "confirmation_number": order.confirmation_number,
                },
            )

    def _resolve_item(self, item_id: str):
        item = self._menu.get_item(item_id)
        if item is None:
            raise MenuItemNotFound(
                f"Menu item '{item_id}' does not exist.",
                {"item_id": item_id},
            )
        if not item.available:
            raise ItemUnavailable(
                f"'{item.name}' is currently unavailable.",
                {"item_id": item_id},
            )
        return item

    @staticmethod
    def _validate_quantity(quantity: int, allow_zero: bool) -> None:
        low = 0 if allow_zero else 1
        if (
            not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or quantity < low
            or quantity > MAX_QUANTITY_PER_LINE
        ):
            raise InvalidQuantity(
                f"Quantity must be an integer between {low} and "
                f"{MAX_QUANTITY_PER_LINE}.",
                {"received": quantity, "min": low, "max": MAX_QUANTITY_PER_LINE},
            )

    def _resolve_modifiers(
        self, item, provided: Dict[str, str], check_required: bool,
        apply_defaults: bool = True,
    ) -> Dict[str, str]:
        """Validate modifier names/options, apply defaults for omitted optionals.

        Returns the normalized modifier dict (defaults included, unless
        apply_defaults is False — used for partial line selectors where a
        default must not silently narrow the match).
        """
        specs = {m.name: m for m in item.modifiers}
        for name in provided:
            if name not in specs:
                raise UnknownModifier(
                    f"'{item.name}' has no modifier named '{name}'.",
                    {"item_id": item.id, "modifier": name,
                     "available_modifiers": sorted(specs)},
                )
        resolved: Dict[str, str] = {}
        for name, spec in specs.items():
            if name in provided:
                option = provided[name]
                if option not in {o.name for o in spec.options}:
                    raise InvalidModifierOption(
                        f"'{option}' is not a valid option for '{name}' "
                        f"on '{item.name}'.",
                        {
                            "item_id": item.id,
                            "modifier": name,
                            "option": option,
                            "valid_options": [o.name for o in spec.options],
                        },
                    )
                resolved[name] = option
            elif spec.required:
                if check_required:
                    raise MissingRequiredModifiers(
                        f"'{item.name}' requires modifier(s): "
                        f"{', '.join(m.name for m in item.modifiers if m.required)}.",
                        {
                            "item_id": item.id,
                            "required_modifiers": [
                                {
                                    "name": m.name,
                                    "options": [o.name for o in m.options],
                                }
                                for m in item.modifiers
                                if m.required
                            ],
                        },
                    )
            elif spec.default is not None and apply_defaults:
                resolved[name] = spec.default
        return resolved

    def _apply_inheritance(
        self, item, order: StoredOrder, provided: Dict[str, str]
    ) -> Dict[str, str]:
        """Documented convenience rule for add:

        When required modifiers are missing from the request but exactly one
        existing order line has this item_id, inherit that line's modifiers
        (the caller may still override individual modifiers).
        """
        missing_required = [
            m.name for m in item.modifiers if m.required and m.name not in provided
        ]
        if not missing_required:
            return provided
        same_item_lines = [ln for ln in order.lines if ln.item_id == item.id]
        if len(same_item_lines) == 1:
            return {**same_item_lines[0].modifiers, **provided}
        raise MissingRequiredModifiers(
            f"'{item.name}' requires modifier(s): {', '.join(missing_required)}.",
            {
                "item_id": item.id,
                "required_modifiers": [
                    {"name": m.name, "options": [o.name for o in m.options]}
                    for m in item.modifiers
                    if m.required
                ],
            },
        )

    @staticmethod
    def _find_line(
        order: StoredOrder, item_id: str, modifiers: Dict[str, str]
    ) -> Optional[StoredOrderLine]:
        for line in order.lines:
            if line.item_id == item_id and line.modifiers == modifiers:
                return line
        return None

    def _resolve_single_line(
        self,
        order: StoredOrder,
        item_id: str,
        modifiers: Optional[Dict[str, str]],
    ) -> StoredOrderLine:
        """Find the one line matching item_id (+ exact modifiers if given).

        Raises LineNotFound (404) or AmbiguousItemReference (409) when the
        selector cannot be resolved to a single line.
        """
        if modifiers:
            item = self._resolve_item(item_id)
            # Validate names/options but do NOT apply defaults: a partial
            # selector such as {"size": "large"} must match any line whose
            # modifiers are a superset of the selector.
            selector = self._resolve_modifiers(
                item, modifiers, check_required=False, apply_defaults=False
            )
            targets = [
                ln
                for ln in order.lines
                if ln.item_id == item_id
                and all(ln.modifiers.get(k) == v for k, v in selector.items())
            ]
            if not targets:
                raise LineNotFound(
                    f"No order line for '{item.name}' matches the given modifiers.",
                    {"item_id": item_id, "modifiers": selector},
                )
            if len(targets) > 1:
                raise AmbiguousItemReference(
                    f"Multiple order lines match '{item_id}'. "
                    "Specify modifiers to choose one.",
                    {
                        "item_id": item_id,
                        "candidates": [
                            {"modifiers": ln.modifiers, "quantity": ln.quantity}
                            for ln in targets
                        ],
                    },
                )
            return targets[0]
        targets = [ln for ln in order.lines if ln.item_id == item_id]
        if not targets:
            raise LineNotFound(
                f"No order line found for item '{item_id}'.",
                {"item_id": item_id},
            )
        if len(targets) > 1:
            raise AmbiguousItemReference(
                f"Multiple order lines match '{item_id}'. "
                "Specify modifiers to choose one.",
                {
                    "item_id": item_id,
                    "candidates": [
                        {"modifiers": ln.modifiers, "quantity": ln.quantity}
                        for ln in targets
                    ],
                },
            )
        return targets[0]

    def _unit_cents(self, item, modifiers: Dict[str, str]) -> int:
        """Backend-authoritative unit price in integer cents."""
        cents = round(item.base_price * 100)
        specs = {m.name: m for m in item.modifiers}
        for name, option_name in modifiers.items():
            spec = specs.get(name)
            if spec is None:
                continue
            for option in spec.options:
                if option.name == option_name:
                    cents += round(option.price_delta * 100)
                    break
        return cents

    def _summarize(self, order: StoredOrder) -> OrderSummary:
        lines: List[OrderLineSummary] = []
        subtotal_cents = 0
        for line in order.lines:
            item = self._menu.get_item(line.item_id)
            if item is None:  # pragma: no cover - menu is static in this POC
                raise MenuItemNotFound(f"Menu item '{line.item_id}' does not exist.")
            unit_cents = self._unit_cents(item, line.modifiers)
            line_cents = unit_cents * line.quantity
            subtotal_cents += line_cents
            lines.append(
                OrderLineSummary(
                    item_id=item.id,
                    name=item.name,
                    quantity=line.quantity,
                    modifiers=dict(line.modifiers),
                    unit_price=round(unit_cents / 100, 2),
                    line_total=round(line_cents / 100, 2),
                )
            )
        return OrderSummary(
            session_id=order.session_id,
            state=order.state,  # type: ignore[arg-type]
            currency=self._menu.currency,
            items=lines,
            item_count=sum(ln.quantity for ln in order.lines),
            subtotal=round(subtotal_cents / 100, 2),
            confirmation_number=order.confirmation_number,
        )
