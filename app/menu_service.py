"""Menu service: loads menu.json once, serves menu reads and search.

The backend is the authoritative source of truth for menu items, prices,
modifiers and availability. The LLM must always consult these methods
(rather than its own knowledge) when menu facts matter.
"""

import difflib
import json
from pathlib import Path
from typing import List, Optional

from app.models import Menu, MenuItem

# Menu file lives next to this package: <repo>/data/menu.json
_MENU_PATH = Path(__file__).resolve().parent.parent / "data" / "menu.json"

# Difflib cutoff used when plain substring search finds nothing.
# 0.7 keeps genuine typos ("chiken" -> "chicken" scores 0.92) while rejecting
# unrelated words ("cheeseburger" -> "cheese" only scores 0.67).
_FUZZY_CUTOFF = 0.7


class MenuService:
    """Loads the Golden Wok menu once at startup and serves queries."""

    def __init__(self, menu_path: Optional[Path] = None) -> None:
        path = Path(menu_path) if menu_path else _MENU_PATH
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        self._menu = Menu.model_validate(raw)
        self._by_id = {item.id: item for item in self._menu.items}

    @property
    def restaurant(self) -> str:
        return self._menu.restaurant

    @property
    def currency(self) -> str:
        return self._menu.currency

    def get_menu(self) -> List[MenuItem]:
        """Return all menu items (in menu.json order)."""
        return list(self._menu.items)

    def get_item(self, item_id: str) -> Optional[MenuItem]:
        """Return one item by id, or None if unknown."""
        return self._by_id.get(item_id)

    def _search_text(self, item: MenuItem) -> str:
        """Lowercased text this item is searchable by.

        Covers name, description, category, plus modifier and option names:
        a query like "chicken" should surface Fried Rice / Lo Mein / Chow Mein
        because their protein options include chicken, even though the word
        does not appear in their descriptions.
        """
        parts = [item.name, item.description, item.category]
        for modifier in item.modifiers:
            parts.append(modifier.name.replace("_", " "))
            parts.extend(option.name for option in modifier.options)
        return " ".join(parts).lower()

    def search_menu(self, query: str) -> List[MenuItem]:
        """Case-insensitive substring search over the item's search text.

        Falls back to difflib.get_close_matches against individual search
        tokens when the substring pass finds nothing. No vector DB involved.
        """
        q = query.strip().lower()
        if not q:
            return []
        matches = [item for item in self._menu.items if q in self._search_text(item)]
        if matches:
            return matches
        # Fuzzy fallback: match the query against individual search tokens so a
        # typo like "chiken" still finds every chicken-related item. The typo
        # is compared to short words, not full item names, which difflib
        # handles much more reliably at the same cutoff.
        tokens = sorted(
            {
                token
                for item in self._menu.items
                for token in self._search_text(item).split()
            }
        )
        close = set(difflib.get_close_matches(q, tokens, n=5, cutoff=_FUZZY_CUTOFF))
        return [
            item
            for item in self._menu.items
            if set(self._search_text(item).split()) & close
        ]
