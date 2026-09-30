"""Tests for MenuService: menu loading and search behavior."""

import pytest

from app.menu_service import MenuService

EXPECTED_IDS = {
    "egg-rolls", "crab-rangoons", "spring-rolls",
    "hot-sour-soup", "wonton-soup",
    "general-tsos-chicken", "kung-pao-chicken", "beef-broccoli",
    "sweet-sour-pork", "mapo-tofu", "sesame-chicken",
    "fried-rice", "white-rice",
    "lo-mein", "chow-mein", "dan-dan-noodles",
    "coke", "diet-coke", "sprite",
}

CHICKEN_IDS = {
    "general-tsos-chicken", "kung-pao-chicken", "sesame-chicken",
    "fried-rice", "lo-mein", "chow-mein",
}


@pytest.fixture()
def menu_service():
    return MenuService()


def test_menu_has_19_items(menu_service):
    items = menu_service.get_menu()
    assert len(items) == 19
    assert {item.id for item in items} == EXPECTED_IDS


def test_search_chicken_finds_chicken_items(menu_service):
    ids = {item.id for item in menu_service.search_menu("chicken")}
    assert CHICKEN_IDS <= ids


def test_search_is_case_insensitive(menu_service):
    lower = {item.id for item in menu_service.search_menu("chicken")}
    upper = {item.id for item in menu_service.search_menu("CHICKEN")}
    assert lower == upper and lower


def test_search_matches_category(menu_service):
    ids = {item.id for item in menu_service.search_menu("drinks")}
    assert {"coke", "diet-coke", "sprite"} <= ids


def test_search_unknown_query_returns_empty(menu_service):
    assert menu_service.search_menu("cheeseburger") == []
    assert menu_service.search_menu("zzzznotreal") == []


def test_search_typo_fuzzy_matches_chicken_items(menu_service):
    ids = {item.id for item in menu_service.search_menu("chiken")}
    assert CHICKEN_IDS <= ids


def test_search_blank_query_returns_empty(menu_service):
    assert menu_service.search_menu("   ") == []


def test_get_item_unknown_returns_none(menu_service):
    assert menu_service.get_item("not-a-real-item") is None
