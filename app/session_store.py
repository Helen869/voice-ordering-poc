"""Session storage abstraction for order state.

The store keeps a small mutable record per session_id. Only the
InMemorySessionStore is provided here; because OrderService depends on the
abstract SessionStore interface, a Firestore/Redis implementation can be
swapped in later without touching any conversational or pricing logic.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class StoredOrderLine(BaseModel):
    """Internal mutable representation of one order line."""

    item_id: str
    modifiers: Dict[str, str] = Field(default_factory=dict)
    quantity: int = Field(ge=0)


class StoredOrder(BaseModel):
    """Internal mutable order record held by the session store."""

    session_id: str
    state: str = "draft"
    lines: List[StoredOrderLine] = Field(default_factory=list)
    confirmation_number: Optional[str] = None


class SessionStore(ABC):
    """Abstract key/value store for per-session order records."""

    @abstractmethod
    def get(self, session_id: str) -> Optional[StoredOrder]:
        """Return the stored order, or None if no session exists yet."""
        raise NotImplementedError

    @abstractmethod
    def save(self, session_id: str, order: StoredOrder) -> None:
        """Persist (insert or replace) the order record for a session."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, session_id: str) -> None:
        """Remove the session's record if present; no-op otherwise."""
        raise NotImplementedError


class InMemorySessionStore(SessionStore):
    """Dict-backed store. Ephemeral: lost on process restart / Cloud Run scale.

    Intentionally non-production. Suitable only for validating the
    conversational flow in this POC.
    """

    def __init__(self) -> None:
        self._orders: Dict[str, StoredOrder] = {}

    def get(self, session_id: str) -> Optional[StoredOrder]:
        return self._orders.get(session_id)

    def save(self, session_id: str, order: StoredOrder) -> None:
        self._orders[session_id] = order

    def delete(self, session_id: str) -> None:
        self._orders.pop(session_id, None)
