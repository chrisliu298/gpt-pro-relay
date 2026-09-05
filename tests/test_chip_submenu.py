"""Regression tests for the 2026-08-28 flat chip menu (Power slider + model list).

The 2026-08-28 ChatGPT redesign REPLACED the two-level "Model/Effort" submenu
with a single flat menu. Clicking the composer chip
(`button.__composer-pill[aria-haspopup="menu"]`) now opens ONE
`[role=menu][data-state="open"]` containing:

    [role=menu][data-state="open"]
      role=menuitem  aria-label="Power"       <- the effort slider handle;
                                                 wraps role=slider 0..4
                                                 (0 Instant .. 4 Pro)
      role=menuitemradio  "Latest"             <- GPT-6; model list is FLAT
      role=menuitemradio  "GPT-5.6 Sol"
      role=menuitemradio  "GPT-5.5"
      role=menuitem  aria-label="Select model" <- view toggle

What broke: the `aria-haspopup="menu"` "Effort" row is gone, so the old
`_open_chip_submenu(EFFORT_ROW)` + `menuitemradio[name="Pro"]` click timed out
(chip_menuitem_missing → model_select_failed). Effort is now a slider, driven by
CLICKING the Power menuitem then pressing `End` — the click commits the
interaction (a bare focus()+ArrowRight reverts on reload, verified live), and
`End` drives to the top tier from any position.

These tests pin:
  - the slow path DRIVES THE SLIDER (click Power + End) and never reaches for
    the removed submenu / named effort radio;
  - it fails closed when the slider never reaches its max OR the chip never
    settles on "Pro";
  - the fast path (chip already "Pro") no-ops without opening the menu;
  - `read_selected_model` reads the checked radio directly from the flat menu,
    returns None when unreadable, and does not toggle an already-open menu shut.
"""

import re

import pytest

from gpt_pro import cli
from gpt_pro.cli import ensure_pro_chip, read_selected_model


# The value the slider must reach to be "Pro" (aria-valuemax). Tier index:
# 0 Instant, 1 Medium, 2 High, 3 Extra High, 4 Pro.
_MAX = 4
_TIER_LABEL = {0: "Instant", 1: "Medium", 2: "High", 3: "Extra High", 4: "Pro"}


class _FakeChip:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    async def wait_for(self, **_kw):
        return None

    async def get_attribute(self, name):
        assert name == "aria-expanded", name
        return "true" if self.page.menu_open else "false"

    async def click(self, timeout=None):
        self.page.clicks.append("chip")
        # Toggle: clicking an open menu closes it.
        self.page.menu_open = not self.page.menu_open

    async def inner_text(self):
        # The chip shows the generic label while the menu is open, and the
        # committed effort tier once it closes — exactly like the live UI.
        if self.page.menu_open:
            return "Thinking effort"
        return _TIER_LABEL[self.page.slider_value]


class _FakePowerMenuItem:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        # Live behaviour: the click engages the slider (and can jump it toward
        # the click position). We model it as "engaged" without moving to max,
        # so only the subsequent End/ArrowRight actually reaches Pro.
        self.page.clicks.append("power")
        self.page.slider_engaged = True


class _FakeMenuLocator:
    """Resolves `[role=menu][data-state="open"]` and evaluates JS against it."""

    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    async def evaluate(self, js):
        if not self.page.menu_open:
            return None
        # SLIDER_STATE_JS: {now, max} or null when no slider.
        if "role=\"slider\"" in js or "aria-valuenow" in js:
            if self.page.has_slider:
                return {"now": str(self.page.slider_value), "max": str(_MAX)}
            return None
        # SELECTED_MODEL_JS: checked radio's text or null.
        if "menuitemradio" in js:
            for label, checked in self.page.model_radios:
                if checked:
                    return label
            return None
        raise AssertionError(f"unexpected evaluate js: {js!r}")


class _FakePage:
    def __init__(
        self,
        *,
        chip_texts=None,
        slider_value=0,
        menu_open=False,
        has_slider=True,
        slider_maxes=True,
        model_radios=(("Latest", True), ("GPT-5.6 Sol", False)),
        chip_settles_pro=True,
    ):
        self.slider_value = slider_value
        self.menu_open = menu_open
        self.has_slider = has_slider
        # When False, End/ArrowRight never advance the slider (models a stuck /
        # missing slider so the drive fails closed).
        self.slider_maxes = slider_maxes
        self.slider_engaged = False
        self.model_radios = list(model_radios)
        # If False, the chip never reads is_pro_label even after a "successful"
        # drive (models the top tier no longer being labeled "Pro").
        self.chip_settles_pro = chip_settles_pro
        self._chip = _FakeChip(self)
        self.clicks = []
        self.keys = []
        self._closed = False

    # ---- Playwright-ish surface ----
    def locator(self, selector):
        if selector == cli.COMPOSER_CHIP:
            return _FakeChip(self)
        if selector == cli.POWER_MENUITEM:
            return _FakePowerMenuItem(self)
        if selector == cli.OPEN_MENU:
            return _FakeMenuLocator(self)
        raise AssertionError(f"unexpected selector {selector!r}")

    def get_by_role(self, *a, **k):
        raise AssertionError("slow path must not use get_by_role (old submenu radio)")

    async def wait_for_selector(self, selector, timeout=None):
        assert selector == cli.OPEN_MENU, selector
        if not self.menu_open:
            raise TimeoutError("menu did not open")
        return None

    async def content(self):
        return "<html></html>"

    def is_closed(self):
        return self._closed

    @property
    def keyboard(self):
        page = self

        class _KB:
            async def press(self, key):
                page.keys.append(key)
                if key in ("End", "ArrowRight") and page.slider_engaged and page.slider_maxes:
                    page.slider_value = _MAX
                if key == "Escape":
                    page.menu_open = False

        return _KB()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Make the 0.2s poll sleeps instant, and advance a FAKE clock by the slept
    # amount so the bounded `time.time()` deadlines in the drive/read loops
    # terminate deterministically instead of busy-spinning real wall-clock.
    clock = {"t": 1_000_000.0}

    async def _instant(delay):
        clock["t"] += max(delay, 0.05)

    monkeypatch.setattr(cli.asyncio, "sleep", _instant)
    monkeypatch.setattr(cli.time, "time", lambda: clock["t"])


@pytest.fixture(autouse=True)
def _stub_slow_path_side_effects(monkeypatch):
    """`ensure_pro_chip`'s slow path takes UiClipboardLock and drives the OS
    focus; stub those so the test exercises only the menu logic."""
    monkeypatch.setattr(cli, "UiClipboardLock", lambda: _NullCtx())
    monkeypatch.setattr(cli, "bind_chrome_compositor_surface", lambda: None)

    async def _noop_front(_page):
        return None

    async def _noop_shot(_page, _path, **_kw):
        return None

    monkeypatch.setattr(cli, "bring_tab_to_front", _noop_front)
    monkeypatch.setattr(cli, "safe_screenshot", _noop_shot)


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---- ensure_pro_chip: fast path ------------------------------------------

async def test_fast_path_noops_when_chip_already_pro(tmp_path):
    # Chip reads "Pro" -> no lock, no menu, no slider drive.
    page = _FakePage(slider_value=_MAX, menu_open=False)
    ok, text = await ensure_pro_chip(page, run_dir=tmp_path)
    assert (ok, text) == (True, "Pro")
    assert page.clicks == []  # never opened the menu
    assert page.keys == []


# ---- ensure_pro_chip: slow path drives the slider ------------------------

async def test_slow_path_drives_slider_to_pro_from_instant(tmp_path):
    # The exact repro: chip stuck on "Instant" (slider value 0). The slow path
    # opens the menu, clicks Power, presses End to reach the max tier, closes
    # the menu, and confirms the chip reads "Pro".
    page = _FakePage(slider_value=0, menu_open=False)
    ok, text = await ensure_pro_chip(page, run_dir=tmp_path)
    assert (ok, text) == (True, "Pro")
    # Drove by click(Power)+End, never a submenu / named radio.
    assert "power" in page.clicks
    assert "End" in page.keys
    assert page.slider_value == _MAX


async def test_slow_path_never_uses_the_removed_submenu_radio(tmp_path):
    # Regression guard: the old mechanism called page.get_by_role(...) to click
    # a `menuitemradio[name="Pro"]`. The fake raises if that path is taken.
    page = _FakePage(slider_value=1, menu_open=False)  # "Medium"
    ok, _ = await ensure_pro_chip(page, run_dir=tmp_path)
    assert ok  # (get_by_role would have raised AssertionError)


async def test_slow_path_fails_closed_when_slider_never_maxes(tmp_path):
    # A stuck/missing slider (End/ArrowRight never advance it) must fail closed
    # with model_select_failed, not spin forever.
    page = _FakePage(slider_value=0, slider_maxes=False)
    ok, text = await ensure_pro_chip(page, run_dir=tmp_path)
    assert ok is False
    assert not cli.is_pro_label(text)


async def test_slow_path_fails_closed_when_slider_absent(tmp_path):
    # SLIDER_STATE_JS returns null (selector break) -> drive can't confirm max
    # -> fail closed.
    page = _FakePage(slider_value=0, has_slider=False)
    ok, _ = await ensure_pro_chip(page, run_dir=tmp_path)
    assert ok is False


async def test_slow_path_fails_closed_when_chip_never_reads_pro(tmp_path):
    # Slider reaches max but the chip label never becomes is_pro_label (models
    # the top tier no longer being called "Pro"): the name-anchored chip gate
    # fails closed even though the slider maxed.
    page = _FakePage(slider_value=0, chip_settles_pro=False)
    # Force the chip to always report a non-Pro label once closed.
    _MonkeyChipText(page, always="Ultra")
    ok, text = await ensure_pro_chip(page, run_dir=tmp_path)
    assert ok is False
    assert not cli.is_pro_label(text)


class _MonkeyChipText:
    """Overrides the closed-menu chip label the fake reports (for the
    'top tier not called Pro' case) without touching the slider mechanics."""

    def __init__(self, page, *, always):
        self._always = always
        orig_locator = page.locator

        def _locator(selector):
            if selector == cli.COMPOSER_CHIP:
                chip = _FakeChip(page)
                orig_inner = chip.inner_text

                async def _inner():
                    if page.menu_open:
                        return "Thinking effort"
                    return self._always

                chip.inner_text = _inner
                return chip
            return orig_locator(selector)

        page.locator = _locator


# ---- read_selected_model: flat model list --------------------------------

async def test_read_selected_model_reads_checked_radio(tmp_path):
    page = _FakePage(menu_open=False,
                     model_radios=(("Latest", True), ("GPT-5.6 Sol", False)))
    assert await read_selected_model(page, timeout=1.0) == "Latest"


async def test_read_selected_model_reads_a_drifted_default(tmp_path):
    # A default drifted to GPT-5.5 must be reported (so doctor/audit can flag it).
    page = _FakePage(menu_open=False,
                     model_radios=(("Latest", False), ("GPT-5.5", True)))
    assert await read_selected_model(page, timeout=1.0) == "GPT-5.5"


async def test_read_selected_model_returns_none_when_no_radio_checked(tmp_path):
    # No checked radio (selector break) -> None -> unverified_missing_slug
    # (fail-open) / doctor "unknown".
    page = _FakePage(menu_open=False,
                     model_radios=(("Latest", False), ("GPT-5.5", False)))
    assert await read_selected_model(page, timeout=0.3) is None


async def test_read_selected_model_returns_none_when_menu_never_opens(tmp_path):
    class _StuckPage(_FakePage):
        async def wait_for_selector(self, selector, timeout=None):
            raise TimeoutError("menu did not open")

    page = _StuckPage(menu_open=False)
    assert await read_selected_model(page, timeout=0.05) is None


async def test_read_selected_model_does_not_toggle_an_already_open_menu(tmp_path):
    # Entering with the menu already open, a blind chip.click() would CLOSE it.
    # _open_chip_menu guards on aria-expanded, so we never click the chip.
    page = _FakePage(menu_open=True,
                     model_radios=(("Latest", True), ("GPT-5.6 Sol", False)))
    assert await read_selected_model(page, timeout=1.0) == "Latest"
    assert "chip" not in page.clicks
