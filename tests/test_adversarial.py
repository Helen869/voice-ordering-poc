"""Adversarial tests: try to break the ordering backend.

Covers the adversarial checklist: nonexistent item, invalid modifier, zero /
negative / very large quantity, repeated add/remove, modifying a line that was
never added, double submit, update after submit, missing/unknown session_id,
malformed requests, ambiguous references.
"""

import pytest

from app.menu_service import MenuService
from app.order_service import (
    AmbiguousItemReference,
    EmptyOrder,
    InvalidModifierOption,
    InvalidQuantity,
    LineNotFound,
    MenuItemNotFound,
    MissingRequiredModifiers,
    OrderLocked,
    OrderService,
    UnknownModifier,
)
from app.session_store import InMemorySessionStore


@pytest.fixture()
def service():
    return OrderService(MenuService(), InMemorySessionStore())


# --- the checklist ---------------------------------------------------------

def test_nonexistent_item(service):
    with pytest.raises(MenuItemNotFound):
        service.add("s", "cheeseburger")


def test_invalid_modifier_name(service):
    with pytest.raises(UnknownModifier):
        service.add("s", "egg-rolls", modifiers={"spice_level": "spicy"})


def test_invalid_modifier_option(service):
    with pytest.raises(InvalidModifierOption):
        service.add("s", "fried-rice", modifiers={"protein": "tofu"})


def test_zero_quantity_add_rejected(service):
    with pytest.raises(InvalidQuantity):
        service.add("s", "egg-rolls", quantity=0)


def test_negative_quantity_rejected(service):
    with pytest.raises(InvalidQuantity):
        service.add("s", "egg-rolls", quantity=-3)


def test_very_large_quantity_rejected(service):
    with pytest.raises(InvalidQuantity):
        service.add("s", "egg-rolls", quantity=999999)


def test_quantity_cap_enforced_on_merge(service):
    service.add("s", "egg-rolls", quantity=20)
    with pytest.raises(InvalidQuantity):
        service.add("s", "egg-rolls", quantity=1)


def test_update_zero_quantity_removes_line(service):
    service.add("s", "egg-rolls", quantity=2)
    summary = service.update("s", "egg-rolls", quantity=0)
    assert summary.items == [] and summary.subtotal == 0


def test_repeated_add_remove_cycle(service):
    service.add("s", "egg-rolls", quantity=2)
    service.remove("s", "egg-rolls", quantity=1)
    summary = service.remove("s", "egg-rolls", quantity=1)
    assert summary.items == []
    # removing again must fail loudly, not silently succeed
    with pytest.raises(LineNotFound):
        service.remove("s", "egg-rolls")


def test_modify_line_never_added(service):
    with pytest.raises(LineNotFound):
        service.update("s", "coke", quantity=2)
    with pytest.raises(LineNotFound):
        service.remove("s", "coke")


def test_submit_twice_idempotent_same_number(service):
    service.add("s", "coke")
    first, already1 = service.submit("s")
    second, already2 = service.submit("s")
    assert (already1, already2) == (False, True)
    assert first.confirmation_number == second.confirmation_number


def test_update_after_submit_locked(service):
    service.add("s", "coke")
    service.submit("s")
    with pytest.raises(OrderLocked):
        service.add("s", "sprite")
    with pytest.raises(OrderLocked):
        service.remove("s", "coke")
    with pytest.raises(OrderLocked):
        service.update("s", "coke", quantity=5)


def test_submit_empty_order_rejected(service):
    with pytest.raises(EmptyOrder):
        service.submit("brand-new-session")


def test_unknown_session_starts_empty_draft(service):
    summary = service.get_order("never-seen")
    assert summary.state == "draft" and summary.items == []


def test_ambiguous_reference_lists_candidates(service):
    service.add("s", "general-tsos-chicken",
                modifiers={"size": "large", "spice_level": "mild"})
    service.add("s", "general-tsos-chicken",
                modifiers={"size": "regular", "spice_level": "mild"})
    with pytest.raises(AmbiguousItemReference) as exc:
        service.remove("s", "general-tsos-chicken")
    cands = exc.value.details["candidates"]
    assert {c["modifiers"]["size"] for c in cands} == {"large", "regular"}


def test_bool_quantity_rejected(service):
    # bool is an int subclass in Python; a JSON `true` must not mean 1.
    with pytest.raises(InvalidQuantity):
        service.add("s", "egg-rolls", quantity=True)


def test_required_modifier_still_required_on_update_override(service):
    # set_modifiers merges over the existing line, so required size survives
    # even when the caller only overrides spice.
    service.add("s", "general-tsos-chicken",
                modifiers={"size": "large", "spice_level": "mild"})
    summary = service.update("s", "general-tsos-chicken",
                             set_modifiers={"spice_level": "spicy"})
    assert summary.items[0].modifiers == {"size": "large", "spice_level": "spicy"}


def test_sessions_do_not_leak(service):
    service.add("s1", "egg-rolls", quantity=2)
    assert service.get_order("s2").items == []


def test_remove_more_than_on_line_removes_line(service):
    service.add("s", "egg-rolls", quantity=2)
    summary = service.remove("s", "egg-rolls", quantity=5)
    assert summary.items == []
