"""Pydantic v2 models for the Golden Wok voice-ordering POC.

These models define the API contract (menu items, tool requests/responses,
order summaries). They carry no business logic.
"""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Menu models
# ---------------------------------------------------------------------------
class ModifierOption(BaseModel):
    """One selectable option within a modifier (e.g. "large" +$3.00)."""

    name: str
    price_delta: float = Field(ge=0, description="Extra charge in USD on top of base_price.")


class ModifierSpec(BaseModel):
    """Definition of a modifier for a menu item (e.g. "size", "spice_level")."""

    name: str
    required: bool
    default: Optional[str] = Field(
        default=None,
        description="Option name applied when the caller omits this optional modifier.",
    )
    options: List[ModifierOption]


class MenuItem(BaseModel):
    """A single item on the Golden Wok menu. Backend is authoritative."""

    id: str
    name: str
    description: str
    category: str
    base_price: float = Field(ge=0)
    available: bool = True
    modifiers: List[ModifierSpec] = Field(default_factory=list)


class Menu(BaseModel):
    restaurant: str
    currency: str
    items: List[MenuItem]


# ---------------------------------------------------------------------------
# Order models
# ---------------------------------------------------------------------------
OrderState = Literal["draft", "submitted"]


class OrderLineSummary(BaseModel):
    """One line in a customer's order, with backend-computed prices."""

    item_id: str
    name: str
    quantity: int
    modifiers: Dict[str, str] = Field(default_factory=dict)
    unit_price: float = Field(description="Price per unit in USD (base + deltas).")
    line_total: float = Field(description="unit_price * quantity, 2dp.")


class OrderSummary(BaseModel):
    """Full order summary returned by get_order / update / submit."""

    session_id: str
    state: OrderState
    currency: str = "USD"
    items: List[OrderLineSummary]
    item_count: int = Field(description="Total quantity across all lines.")
    subtotal: float = Field(description="Sum of line totals, 2dp.")
    confirmation_number: Optional[str] = None
    already_submitted: Optional[bool] = Field(
        default=None, description="Present and true on idempotent repeat submit."
    )


# ---------------------------------------------------------------------------
# Tool request models
# ---------------------------------------------------------------------------
OrderAction = Literal["add", "remove", "update"]


class OrderUpdateRequest(BaseModel):
    session_id: str = Field(min_length=1)
    action: OrderAction
    item_id: str = Field(min_length=1)
    quantity: Optional[int] = None
    modifiers: Optional[Dict[str, str]] = Field(
        default=None, description="Modifier selector for add (match selector for remove)."
    )
    set_modifiers: Optional[Dict[str, str]] = Field(
        default=None, description="Modifier overrides for update."
    )


class OrderSubmitRequest(BaseModel):
    session_id: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------
class ErrorBody(BaseModel):
    code: str = Field(description="snake_case error code, e.g. item_not_found.")
    message: str = Field(description="Human- and LLM-readable explanation.")
    details: Dict = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorBody
