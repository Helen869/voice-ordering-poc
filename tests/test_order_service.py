"""Tests for OrderService: validation, mutation, pricing, submission."""

import re

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
    NothingToUpdate,
    OrderLocked,
    OrderService,
)
from app.session_store import InMemorySessionStore


@pytest.fixture()
def service():
    return OrderService(MenuService(), InMemorySessionStore())


def test_add_valid_line_computes_prices(service):
    summary = service.add("s1", "egg-rolls", quantity=2)
    assert summary.item_count == 2
    assert summary.subtotal == 3.5
    (line,) = summary.items
    assert line.item_id == "egg-rolls"
    assert line.quantity == 2
    assert line.unit_price == 1.75
    assert line.line_total == 3.5


def test_add_invalid_item_raises(service):
    with pytest.raises(MenuItemNotFound):
        service.add("s1", "cheeseburger")


def test_add_invalid_modifier_option_raises(service):
    with pytest.raises(InvalidModifierOption):
        service.add("s1", "general-tsos-chicken",
                    modifiers={"size": "mega", "spice_level": "spicy"})


def test_add_missing_required_modifier_names_them(service):
    with pytest.raises(MissingRequiredModifiers) as exc_info:
        service.add("s1", "fried-rice")
    required = exc_info.value.details["required_modifiers"]
    assert [r["name"] for r in required] == ["protein"]


def test_add_inherits_modifiers_from_unique_existing_line(service):
    service.add("s1", "lo-mein", modifiers={"protein": "chicken"})
    # "add another chicken lo mein" without modifiers: merges with existing line.
    summary = service.add("s1", "lo-mein")
    assert len(summary.items) == 1
    assert summary.items[0].modifiers == {"protein": "chicken"}
    assert summary.items[0].quantity == 2


def test_quantity_math_large_general_tsos_spicy(service):
    summary = service.add(
        "s1", "general-tsos-chicken", quantity=2,
        modifiers={"size": "large", "spice_level": "spicy"},
    )
    (line,) = summary.items
    assert line.unit_price == 16.99  # 13.99 + 3.00
    assert line.line_total == 33.98
    assert summary.subtotal == 33.98


def test_add_merges_identical_line(service):
    service.add("s1", "egg-rolls", quantity=2)
    summary = service.add("s1", "egg-rolls", quantity=3)
    assert len(summary.items) == 1
    assert summary.items[0].quantity == 5
    assert summary.subtotal == 8.75


def test_add_exceeding_per_line_cap_raises(service):
    service.add("s1", "egg-rolls", quantity=20)
    with pytest.raises(InvalidQuantity):
        service.add("s1", "egg-rolls", quantity=1)


def test_add_invalid_quantities_raise(service):
    for bad in (0, -2, 21, 999):
        with pytest.raises(InvalidQuantity):
            service.add("s1", "egg-rolls", quantity=bad)


def test_update_quantity(service):
    service.add("s1", "egg-rolls", quantity=2)
    summary = service.update("s1", "egg-rolls", quantity=3)
    assert summary.items[0].quantity == 3
    assert summary.subtotal == 5.25


def test_update_set_modifiers(service):
    service.add("s1", "general-tsos-chicken", quantity=1,
                modifiers={"size": "regular", "spice_level": "medium"})
    summary = service.update("s1", "general-tsos-chicken",
                             set_modifiers={"spice_level": "spicy"})
    assert summary.items[0].modifiers["spice_level"] == "spicy"
    assert summary.items[0].modifiers["size"] == "regular"


def test_update_nothing_to_update_raises(service):
    service.add("s1", "egg-rolls", quantity=1)
    with pytest.raises(NothingToUpdate):
        service.update("s1", "egg-rolls")


def test_update_quantity_zero_deletes_line(service):
    service.add("s1", "egg-rolls", quantity=2)
    summary = service.update("s1", "egg-rolls", quantity=0)
    assert summary.items == []
    assert summary.subtotal == 0


def test_remove_partial_quantity(service):
    service.add("s1", "egg-rolls", quantity=3)
    summary = service.remove("s1", "egg-rolls", quantity=1)
    assert summary.items[0].quantity == 2


def test_remove_whole_line_when_quantity_omitted(service):
    service.add("s1", "egg-rolls", quantity=2)
    service.add("s1", "coke", quantity=1)
    summary = service.remove("s1", "egg-rolls")
    assert [ln.item_id for ln in summary.items] == ["coke"]


def test_remove_more_than_present_deletes_line(service):
    service.add("s1", "egg-rolls", quantity=2)
    summary = service.remove("s1", "egg-rolls", quantity=5)
    assert summary.items == []


def test_remove_unknown_line_raises(service):
    service.add("s1", "egg-rolls", quantity=1)
    with pytest.raises(LineNotFound):
        service.remove("s1", "coke")


def test_ambiguous_remove_without_selector_raises(service):
    service.add("s1", "lo-mein", modifiers={"protein": "chicken"})
    service.add("s1", "lo-mein", modifiers={"protein": "shrimp"})
    with pytest.raises(AmbiguousItemReference) as exc_info:
        service.remove("s1", "lo-mein")
    candidates = exc_info.value.details["candidates"]
    assert len(candidates) == 2
    assert {c["quantity"] for c in candidates} == {1}


def test_remove_with_modifier_selector_disambiguates(service):
    service.add("s1", "lo-mein", modifiers={"protein": "chicken"})
    service.add("s1", "lo-mein", modifiers={"protein": "shrimp"})
    summary = service.remove("s1", "lo-mein", modifiers={"protein": "shrimp"})
    assert [ln.modifiers for ln in summary.items] == [{"protein": "chicken"}]


def test_optional_modifier_default_applied(service):
    summary = service.add("s1", "coke", quantity=1)
    assert summary.items[0].modifiers == {"size": "regular"}
    assert summary.items[0].unit_price == 2.99


def test_submit_empty_order_raises(service):
    with pytest.raises(EmptyOrder):
        service.submit("empty-session")


def test_submit_success_returns_confirmation_number(service):
    service.add("s1", "egg-rolls", quantity=2)
    summary, already = service.submit("s1")
    assert already is False
    assert summary.state == "submitted"
    assert re.fullmatch(r"GW-\d{4,}", summary.confirmation_number or "")
    assert summary.subtotal == 3.5


def test_submit_twice_is_idempotent(service):
    service.add("s1", "egg-rolls", quantity=1)
    first, already_first = service.submit("s1")
    second, already_second = service.submit("s1")
    assert already_first is False
    assert already_second is True
    assert second.confirmation_number == first.confirmation_number
    assert second.state == "submitted"


def test_update_after_submit_raises(service):
    service.add("s1", "egg-rolls", quantity=1)
    service.submit("s1")
    with pytest.raises(OrderLocked):
        service.add("s1", "coke", quantity=1)
    with pytest.raises(OrderLocked):
        service.remove("s1", "egg-rolls")


def test_sessions_are_isolated(service):
    service.add("s-a", "egg-rolls", quantity=1)
    service.add("s-b", "coke", quantity=2)
    assert service.get_order("s-a").item_count == 1
    assert service.get_order("s-b").item_count == 2


def test_get_order_unknown_session_returns_empty_draft(service):
    summary = service.get_order("brand-new-session")
    assert summary.state == "draft"
    assert summary.items == []
    assert summary.subtotal == 0
    assert summary.confirmation_number is None


def test_remove_with_partial_modifier_selector(service):
    # "remove the large General Tso's" — selector need not name every modifier.
    service.add("s1", "general-tsos-chicken",
                modifiers={"size": "large", "spice_level": "spicy"})
    service.add("s1", "general-tsos-chicken",
                modifiers={"size": "regular", "spice_level": "mild"})
    summary = service.remove("s1", "general-tsos-chicken",
                             modifiers={"size": "large"})
    assert len(summary.items) == 1
    assert summary.items[0].modifiers == {"size": "regular", "spice_level": "mild"}


def test_partial_selector_matching_multiple_lines_is_ambiguous(service):
    service.add("s1", "general-tsos-chicken",
                modifiers={"size": "large", "spice_level": "mild"})
    service.add("s1", "general-tsos-chicken",
                modifiers={"size": "regular", "spice_level": "mild"})
    with pytest.raises(AmbiguousItemReference) as exc_info:
        service.remove("s1", "general-tsos-chicken",
                       modifiers={"spice_level": "mild"})
    assert len(exc_info.value.details["candidates"]) == 2


def test_update_with_partial_selector(service):
    # "make the spicy one mild"
    service.add("s1", "general-tsos-chicken",
                modifiers={"size": "large", "spice_level": "spicy"})
    summary = service.update("s1", "general-tsos-chicken",
                             modifiers={"spice_level": "spicy"},
                             set_modifiers={"spice_level": "mild"})
    assert summary.items[0].modifiers == {"size": "large", "spice_level": "mild"}


def test_partial_selector_with_invalid_option_raises(service):
    service.add("s1", "general-tsos-chicken",
                modifiers={"size": "large", "spice_level": "spicy"})
    with pytest.raises(InvalidModifierOption):
        service.remove("s1", "general-tsos-chicken",
                       modifiers={"size": "mega"})
