"""
A minimal fake implementing just enough of Playwright's sync Page/Locator
surface for ReplayEngine and DiscoveryAgent unit tests to run without a
real browser. Backed by a tiny in-memory "DOM" of dict nodes.

This is intentionally not a full Playwright shim — it exists to test OUR
logic (locator fallback order, business-outcome detection, guardrail
enforcement, escalation control-transfer), not to test Playwright itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


class ElementNotFound(Exception):
    pass


@dataclass
class FakeElement:
    role: str = ""
    name: str = ""
    text: str = ""
    css_id: Optional[str] = None
    css_selector_match: Optional[str] = None  # what CSS selectors should match this
    value: str = ""

    def click(self, timeout=None):
        self.page._on_click(self)  # type: ignore[attr-defined]

    def fill(self, value, timeout=None):
        self.value = value
        self.page._on_fill(self, value)  # type: ignore[attr-defined]

    def select_option(self, value, timeout=None):
        self.value = value
        self.page._on_select(self, value)  # type: ignore[attr-defined]

    def inner_text(self):
        return self.text

    def wait_for(self, state="visible", timeout=None):
        return None


class FakeLocator:
    def __init__(self, page: "FakePage", elements: list[FakeElement]):
        self.page = page
        self._elements = elements
        for e in elements:
            e.page = page  # type: ignore[attr-defined]

    @property
    def first(self):
        if not self._elements:
            raise ElementNotFound("no matching element")
        return self._elements[0]


class FakePage:
    """
    `state` is a simple string key naming which mock "screen" we're on
    (e.g. "search_form", "member_record", "not_found", "confirmation").
    Tests drive state transitions via on_click/on_fill callbacks.
    """

    def __init__(self):
        self.state = "search_form"
        self.url = ""
        self.on_click_handler = None
        self.on_fill_handler = None
        self.on_select_handler = None
        self._screens: dict[str, dict[str, Any]] = {}

    def goto(self, url, timeout=None):
        self.url = url

    def content(self) -> str:
        screen = self._screens.get(self.state, {})
        return screen.get("html", "")

    def get_by_role(self, role, name=None):
        return self._match(role=role, name=name)

    def get_by_text(self, text):
        return self._match(text=text)

    def get_by_label(self, label):
        return self._match(name=label)

    def get_by_test_id(self, test_id):
        return self._match(css_id=test_id)

    def locator(self, css_selector):
        return self._match(css_selector=css_selector)

    def _match(self, role=None, name=None, text=None, css_id=None, css_selector=None) -> FakeLocator:
        screen = self._screens.get(self.state, {})
        elements: list[FakeElement] = screen.get("elements", [])
        matches = []
        for e in elements:
            if role is not None and e.role != role:
                continue
            if name is not None:
                pattern = name.pattern if hasattr(name, "pattern") else name
                if not re.search(pattern, e.name or ""):
                    continue
            if text is not None and text not in (e.text or ""):
                continue
            if css_id is not None and e.css_id != css_id:
                continue
            if css_selector is not None and e.css_selector_match != css_selector:
                continue
            matches.append(e)
        return FakeLocator(self, matches)

    def _on_click(self, element: FakeElement):
        if self.on_click_handler:
            self.on_click_handler(self, element)

    def _on_fill(self, element: FakeElement, value: str):
        if self.on_fill_handler:
            self.on_fill_handler(self, element, value)

    def _on_select(self, element: FakeElement, value: str):
        if self.on_select_handler:
            self.on_select_handler(self, element, value)
