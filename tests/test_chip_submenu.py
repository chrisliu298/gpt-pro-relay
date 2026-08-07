"""Regression tests for the 2026-08 two-level chip menu navigation.

The redesign replaced the flat "Intelligence" effort list with a top-level
"Power" slider plus two submenu rows behind an "Advanced" toggle:

    [role=menu]  Power(slider) / Advanced / Model > / Effort >
                                                        └─ Instant .. Pro

Three things broke at once, and each is pinned below:

1. The named effort leaves moved one level deeper, so `ensure_pro_chip`'s
   direct `menuitemradio[name="Pro"]` click found nothing → model_select_failed
   (fail-closed, but the tool could not run at all).
2. The rows only appear once "Advanced" is expanded, and that expansion resets
   to compact on every page load — so it must be redone every run.
3. The rows open on CLICK, not hover: each row is a wrapper div whose inner
   button swallows pointer events ("subtree intercepts pointer events"), so the
   old hover-driven `read_selected_model` silently returned None. That one was
   the dangerous break — a None model read makes the post-send audit fall back
   to `unverified_missing_slug`, which fails OPEN.

The `count() >= 2` mount guard is the other load-bearing piece: `[role=menu].last`
before the submenu mounts is the MAIN menu, whose checked radio is the *effort*
tier, not the model. Reading it would feed "Pro" to `classify_served_audit` as
if it were a model name → `menu_mismatch` → a fatal verdict on a healthy run.
"""

import re

import pytest

from gpt_pro import cli
from gpt_pro.cli import (
    ADVANCED_EXPAND_LABEL,
    EFFORT_ROW,
    MODEL_ROW,
    _open_chip_submenu,
    read_selected_model,
)


class _FakeElement:
    """A menu row. Hover raises the way the real intercepted wrapper does."""

    def __init__(self, page, label):
        self._page = page
        self._label = label

    async def click(self, timeout=None):
        self._page.clicks.append(self._label)
        self._page.on_click(self._label)

    async def hover(self, **_kw):
        # The real row's inner button intercepts pointer events, so Playwright's
        # hover never lands. Any code that reaches for hover again must fail.
        self._page.hovers.append(self._label)
        raise TimeoutError("subtree intercepts pointer events")


class _FakeLocator:
    def __init__(self, page, kind, *, items):
        self._page = page
        self._kind = kind
        self._items = items

    async def count(self):
        if self._kind == "menus":
            return self._page.menu_count()
        return len(self._items)

    def filter(self, has_text=None):
        keep = [i for i in self._items if has_text is None or has_text.search(i)]
        return _FakeLocator(self._page, self._kind, items=keep)

    @property
    def first(self):
        return _FakeElement(self._page, self._items[0])

    @property
    def last(self):
        # For the "menus" locator this is what the caller receives as the
        # submenu — identity matters, so hand back a tagged marker.
        return _FakeSubmenu(self._page, self._page.menu_stack[-1])


class _FakeSubmenu:
    def __init__(self, page, name):
        self._page = page
        self.name = name

    async def evaluate(self, _js):
        return self._page.checked_radio.get(self.name)

    def get_by_role(self, role, name=None):
        return _FakeElement(self._page, f"{self.name}:{role}:{getattr(name, 'pattern', name)}")


class _FakeMenuPage:
    """Models the new chip menu: compact/expanded state and submenu mounting."""

    def __init__(
        self,
        *,
        compact=True,
        rows=("Model\nGPT-5.6 Sol", "Effort\nMedium"),
        mounts_submenu=True,
        expand_raises=False,
    ):
        self.compact = compact
        self.rows = list(rows)
        self.mounts_submenu = mounts_submenu
        self.expand_raises = expand_raises
        self.clicks = []
        self.hovers = []
        self.keys = []
        self.menu_stack = ["main"]
        self.checked_radio = {"main": "Pro", "model-submenu": "GPT-5.6 Sol"}
        self._closed = False

    # -- state transitions driven by clicks
    def on_click(self, label):
        if label == ADVANCED_EXPAND_LABEL:
            if self.expand_raises:
                raise TimeoutError("advanced toggle not clickable")
            self.compact = False
        elif label in self.rows and self.mounts_submenu:
            self.menu_stack = ["main", "model-submenu"]

    def menu_count(self):
        return len(self.menu_stack)

    # -- Playwright surface
    def locator(self, selector):
        if selector == '[role="menu"]':
            return _FakeLocator(self, "menus", items=self.menu_stack)
        if ADVANCED_EXPAND_LABEL in selector:
            items = [ADVANCED_EXPAND_LABEL] if self.compact else []
            return _FakeLocator(self, "advanced", items=items)
        if 'aria-haspopup="menu"' in selector:
            # Rows exist in the DOM only once expanded.
            return _FakeLocator(self, "rows", items=[] if self.compact else self.rows)
        raise AssertionError(f"unexpected selector {selector!r}")

    def is_closed(self):
        return self._closed

    async def wait_for_selector(self, _selector, timeout=None):
        return None

    @property
    def keyboard(self):
        page = self

        class _KB:
            async def press(self, key):
                page.keys.append(key)

        return _KB()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_delay):
        return None

    monkeypatch.setattr(cli.asyncio, "sleep", _instant)


async def test_expands_advanced_before_reaching_the_rows():
    # The rows live behind the "Advanced" toggle, which resets to compact on
    # every page load — so every run must expand before it can click a row.
    page = _FakeMenuPage(compact=True)
    await _open_chip_submenu(page, EFFORT_ROW)
    assert page.clicks[0] == ADVANCED_EXPAND_LABEL
    assert page.clicks[1] == "Effort\nMedium"


async def test_expand_is_skipped_when_already_expanded():
    # The expand locator keys on the DIRECTIONAL aria-label, so it matches only
    # in the compact state. A blind toggle would COLLAPSE an open menu here.
    page = _FakeMenuPage(compact=False)
    await _open_chip_submenu(page, EFFORT_ROW)
    assert ADVANCED_EXPAND_LABEL not in page.clicks
    assert page.clicks == ["Effort\nMedium"]


async def test_rows_are_opened_by_click_never_hover():
    # The pre-2026-08 code hovered; the wrapper row's inner button intercepts
    # pointer events so hover never lands. Any regression to hover fails here.
    page = _FakeMenuPage(compact=False)
    await _open_chip_submenu(page, MODEL_ROW)
    assert page.hovers == []
    assert page.clicks == ["Model\nGPT-5.6 Sol"]


async def test_selects_the_requested_row():
    # Two rows carry aria-haspopup="menu"; the pattern must disambiguate rather
    # than relying on document order.
    page = _FakeMenuPage(compact=False)
    await _open_chip_submenu(page, MODEL_ROW)
    assert page.clicks == ["Model\nGPT-5.6 Sol"]

    page2 = _FakeMenuPage(compact=False)
    await _open_chip_submenu(page2, EFFORT_ROW)
    assert page2.clicks == ["Effort\nMedium"]


async def test_returns_the_submenu_not_the_main_menu():
    page = _FakeMenuPage(compact=False)
    submenu = await _open_chip_submenu(page, MODEL_ROW)
    assert submenu.name == "model-submenu"


async def test_raises_when_the_submenu_never_mounts():
    # Fail closed. Returning `[role=menu].last` here would hand back the MAIN
    # menu, whose checked radio is the EFFORT tier ("Pro") — which
    # classify_served_audit would then read as a model name and call
    # menu_mismatch, a FATAL verdict on a perfectly healthy run.
    page = _FakeMenuPage(compact=False, mounts_submenu=False)
    with pytest.raises(RuntimeError, match="did not mount"):
        await _open_chip_submenu(page, MODEL_ROW, timeout=0.05)


async def test_expansion_failure_is_best_effort():
    # Navigation is forgiving, verification is not: a relabelled/unclickable
    # "Advanced" toggle must not abort the send path, because the real gates are
    # downstream (chip must read Pro; served slug must be allowlisted).
    page = _FakeMenuPage(compact=True, expand_raises=True)
    page.compact = False  # rows reachable anyway (e.g. a variant without the toggle)
    submenu = await _open_chip_submenu(page, MODEL_ROW)
    assert submenu.name == "model-submenu"


async def test_read_selected_model_reads_the_submenu_radio():
    page = _FakeMenuPage(compact=True)
    # read_selected_model clicks the chip itself first.
    page.locator = _with_chip(page)
    assert await read_selected_model(page, timeout=1.0) == "GPT-5.6 Sol"


async def test_read_selected_model_returns_none_when_submenu_never_mounts():
    # Degrades to None ("unknown" in doctor) rather than reporting the main
    # menu's checked EFFORT tier as the model.
    page = _FakeMenuPage(compact=True, mounts_submenu=False)
    page.locator = _with_chip(page)
    assert await read_selected_model(page, timeout=0.05) is None


def _with_chip(page):
    """Wrap page.locator so the COMPOSER_CHIP selector resolves to a clickable."""
    inner = page.locator

    def _locator(selector):
        if selector == cli.COMPOSER_CHIP:
            return _FakeLocator(page, "chip", items=["chip"])
        return inner(selector)

    return _locator
