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


# -- token pass: item name plus that item's own option values -------------------------
import pytest


@pytest.mark.parametrize("query,expected", [
    ("beef lo mein", {"lo-mein"}),
    ("chicken lo mein", {"lo-mein"}),
    ("shrimp fried rice", {"fried-rice"}),
    ("Vegetable Chow Mein", {"chow-mein"}),
    ("large General Tso's chicken", {"general-tsos-chicken"}),
    ("spicy kung pao chicken", {"kung-pao-chicken"}),
    ("large coke", {"coke"}),
])
def test_search_name_plus_option_values(menu_service, query, expected):
    assert {item.id for item in menu_service.search_menu(query)} == expected


@pytest.mark.parametrize("query", [
    "pork lo mein",            # pork is not a lo mein option
    "large fried rice",        # size is not a fried rice modifier
    "spicy egg rolls",         # egg rolls have no modifiers
    "beef lo mein with extra sauce",  # extra words that are not option values
])
def test_search_extra_tokens_must_be_that_items_options(menu_service, query):
    assert menu_service.search_menu(query) == []


def test_search_substring_results_unchanged(menu_service):
    # Queries the substring pass already answered keep their old results.
    assert {i.id for i in menu_service.search_menu("lo mein")} == {"lo-mein"}
    assert {i.id for i in menu_service.search_menu("fried rice")} == {"fried-rice"}
    assert len(menu_service.search_menu("chicken")) > 3


# -- exact name matches rank first ------------------------------------------------------
@pytest.mark.parametrize("query,first", [
    ("coke", "coke"),
    ("Coke", "coke"),
    ("diet coke", "diet-coke"),
    ("General Tso's Chicken", "general-tsos-chicken"),
    ("general tsos chicken", "general-tsos-chicken"),
])
def test_exact_name_match_ranks_first(menu_service, query, first):
    results = menu_service.search_menu(query)
    assert results[0].id == first
    assert menu_service.is_exact_match(results[0], query)
    assert not any(menu_service.is_exact_match(i, query) for i in results[1:])


def test_coke_query_still_lists_diet_coke_but_not_as_exact(menu_service):
    results = menu_service.search_menu("coke")
    assert [i.id for i in results] == ["coke", "diet-coke"]
    assert not menu_service.is_exact_match(results[1], "coke")


def test_partial_name_is_not_exact(menu_service):
    assert not any(menu_service.is_exact_match(i, "chicken") for i in menu_service.search_menu("chicken"))
    assert not menu_service.is_exact_match(menu_service.get_item("lo-mein"), "beef lo mein")
