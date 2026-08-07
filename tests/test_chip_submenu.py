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
   old hover-driven `read_selected_model` silently returned None.

The bulk of these tests, though, pin SUBMENU IDENTITY, because that is where a
wrong answer can escape. Radix keeps a dismissed menu mounted through its exit
animation, so "is there a second [role=menu]?" is not the same question as "is
the menu this row owns open?". Resolving the wrong one fails in two directions:

  - the MAIN menu holds a slider and no checked menuitemradio, so it reads as
    None → `unverified_missing_slug`, which fails OPEN (backstop silently lost);
  - a stale EFFORT submenu reads "Pro" as a model name → `menu_mismatch`, a
    FATAL verdict on a healthy run.

The fake therefore models menu ids, `data-state` (open vs closing), per-row
`aria-controls` ownership, delayed mounting, and pre-existing menus — an
implementation that resolves `[role=menu].last` fails these.
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


class _Menu:
    def __init__(self, menu_id, *, state="open", radio=None):
        self.id = menu_id
        self.state = state
        self.radio = radio  # innerText of the aria-checked="true" menuitemradio


class _Row:
    def __init__(self, page, label, *, submenu_id, radio, opens_after=0, sets_controls=True):
        self.page = page
        self.label = label
        self.submenu_id = submenu_id
        self.radio = radio
        self.expanded = False
        self.opens_after = opens_after  # clicks->polls of delay before it mounts
        self.sets_controls = sets_controls
        self._pending = None

    async def click(self, timeout=None):
        self.page.clicks.append(self.label)
        if self.expanded:
            # Radix toggles: clicking an expanded row collapses it. Production
            # must not do this — the re-entrancy test asserts we never get here.
            self.expanded = False
            self.page.menus = [m for m in self.page.menus if m.id != self.submenu_id]
            return
        self._pending = self.opens_after
        if self._pending == 0:
            self._mount()

    def _mount(self):
        self.expanded = True
        self.page.menus.append(_Menu(self.submenu_id, radio=self.radio))

    def tick(self):
        if self._pending:
            self._pending -= 1
            if self._pending == 0:
                self._mount()

    async def get_attribute(self, name):
        if name == "aria-expanded":
            return "true" if self.expanded else "false"
        if name == "aria-controls":
            return self.submenu_id if (self.expanded and self.sets_controls) else None
        raise AssertionError(f"unexpected attribute {name!r}")

    async def hover(self, **_kw):
        # The real row's inner button intercepts pointer events, so hover never
        # lands. Any code that reaches for hover again must fail loudly.
        self.page.hovers.append(self.label)
        raise TimeoutError("subtree intercepts pointer events")


class _ExpandItem:
    def __init__(self, page):
        self.page = page

    async def click(self, timeout=None):
        self.page.clicks.append(ADVANCED_EXPAND_LABEL)
        if self.page.expand_raises:
            raise TimeoutError("advanced toggle not clickable")
        self.page.compact = False


class _MenuLocator:
    """Resolves menus by id (and optionally by open state)."""

    def __init__(self, page, menu_id, *, require_open):
        self.page = page
        self.menu_id = menu_id
        self.require_open = require_open

    def _match(self):
        return [
            m
            for m in self.page.menus
            if m.id == self.menu_id and (not self.require_open or m.state == "open")
        ]

    async def count(self):
        self.page.tick()
        return len(self._match())

    async def evaluate(self, _js):
        found = self._match()
        return found[0].radio if found else None

    def get_by_role(self, role, name=None):
        return _RadioItem(self.page, self.menu_id, role, getattr(name, "pattern", name))


class _RadioItem:
    def __init__(self, page, menu_id, role, name):
        self.page = page
        self.menu_id = menu_id
        self.role = role
        self.name = name

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        self.page.clicks.append(f"{self.menu_id}:{self.role}:{self.name}")


class _MenuListLocator:
    """All `[role=menu]` nodes, INCLUDING ones closing through their exit
    animation — which is precisely why document order is not identity."""

    def __init__(self, page):
        self.page = page

    async def count(self):
        # Ticking here too keeps the harness FAIR: a delayed mount advances for
        # any implementation that polls, not only for one that calls evaluate().
        self.page.tick()
        return len(self.page.menus)

    @property
    def last(self):
        # Radix portals append, so the newest menu really is last in document
        # order — which is why `.last` looks right until a mount is delayed.
        return _MenuLocator(self.page, self.page.menus[-1].id, require_open=False)


class _RowLocator:
    def __init__(self, page, rows):
        self.page = page
        self.rows = rows

    async def count(self):
        return len(self.rows)

    def filter(self, has_text=None):
        keep = [r for r in self.rows if has_text is None or has_text.search(r.label)]
        return _RowLocator(self.page, keep)

    @property
    def first(self):
        return self.rows[0]


class _ChipLocator:
    """The chip is a TOGGLE — clicking it while open closes the menu."""

    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    async def get_attribute(self, name):
        assert name == "aria-expanded", name
        return "true" if self.page.chip_open else "false"

    async def click(self, timeout=None):
        self.page.clicks.append("chip")
        if self.page.chip_open:
            # Closing the chip menu tears down the whole tree: what is left
            # behind is only menus in their exit animation.
            self.page.chip_open = False
            for m in self.page.menus:
                m.state = "closed"
            for r in self.page.rows:
                r.expanded = False
        else:
            self.page.chip_open = True


class _FakeMenuPage:
    """Models the redesigned chip menu: ids, open/closing state, ownership."""

    MAIN_ID = "menu-main"

    def __init__(
        self,
        *,
        compact=True,
        expand_raises=False,
        extra_menus=(),
        opens_after=0,
        sets_controls=True,
        main_radio=None,
        chip_open=False,
    ):
        self.compact = compact
        self.expand_raises = expand_raises
        self.chip_open = chip_open
        self.clicks = []
        self.hovers = []
        self.keys = []
        # The MAIN menu really has a slider and NO checked radio (2026-08); the
        # default None models that. Tests override it to prove we never read it.
        self.menus = [_Menu(self.MAIN_ID, radio=main_radio), *extra_menus]
        self.rows = [
            _Row(self, "Model\nGPT-5.6 Sol", submenu_id="menu-model",
                 radio="GPT-5.6 Sol", opens_after=opens_after, sets_controls=sets_controls),
            _Row(self, "Effort\nMedium", submenu_id="menu-effort",
                 radio="Pro", opens_after=opens_after, sets_controls=sets_controls),
        ]
        self._closed = False

    def locator(self, selector):
        if selector == cli.COMPOSER_CHIP:
            return _ChipLocator(self)
        if ADVANCED_EXPAND_LABEL in selector:
            return _RowLocator(self, [_ExpandItem(self)] if self.compact else [])
        if 'aria-haspopup="menu"' in selector:
            # Rows are reachable only once expanded.
            return _RowLocator(self, [] if self.compact else self.rows)
        m = re.match(r'\[role="menu"\]\[id="([^"]+)"\](\[data-state="open"\])?$', selector)
        if m:
            return _MenuLocator(self, m.group(1), require_open=bool(m.group(2)))
        if selector == '[role="menu"]':
            # Deliberately supported so a regression to the old global
            # `count() >= 2` / `.last` resolution fails on BEHAVIOUR (it returns
            # the wrong menu's contents) rather than on an unsupported-selector
            # AssertionError, which would be a harness artifact, not a catch.
            return _MenuListLocator(self)
        raise AssertionError(f"unexpected selector {selector!r}")

    def tick(self):
        for row in self.rows:
            row.tick()  # each poll advances a delayed mount

    async def evaluate(self, js):
        assert "data-state=" in js and "role=" in js, js
        self.tick()
        return [m.id for m in self.menus if m.state == "open"]

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


# ---- navigation mechanics -------------------------------------------------

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
    assert page.clicks == ["Effort\nMedium"]


async def test_rows_are_opened_by_click_never_hover():
    # The pre-2026-08 code hovered; the wrapper row's inner button intercepts
    # pointer events so hover never lands. Any regression to hover fails here.
    page = _FakeMenuPage(compact=False)
    await _open_chip_submenu(page, MODEL_ROW)
    assert page.hovers == []
    assert page.clicks == ["Model\nGPT-5.6 Sol"]


async def test_returns_the_submenu_owned_by_the_requested_row():
    # Two rows carry aria-haspopup="menu". The returned menu must be the one
    # THAT row owns — not merely "a second menu".
    page = _FakeMenuPage(compact=False)
    assert await (await _open_chip_submenu(page, MODEL_ROW)).evaluate("") == "GPT-5.6 Sol"

    page2 = _FakeMenuPage(compact=False)
    assert await (await _open_chip_submenu(page2, EFFORT_ROW)).evaluate("") == "Pro"


async def test_resolves_by_ownership_when_a_stale_closing_menu_is_attached():
    # Radix keeps a dismissed menu mounted through its exit animation. A global
    # `.last` / `count() >= 2` can resolve THAT one. Here a stale effort submenu
    # is still attached (data-state="closed") when we open the Model row.
    stale = _Menu("menu-effort-stale", state="closed", radio="Pro")
    page = _FakeMenuPage(compact=False, extra_menus=[stale])
    submenu = await _open_chip_submenu(page, MODEL_ROW)
    # "Pro" here would be the stale EFFORT menu read as a model name ->
    # menu_mismatch, a fatal verdict on a healthy run.
    assert await submenu.evaluate("") == "GPT-5.6 Sol"


async def test_ignores_an_unrelated_menu_that_is_already_open():
    # A second menu open for an unrelated reason must not be mistaken for the
    # submenu: it existed BEFORE the click, so it is not "freshly opened".
    unrelated = _Menu("menu-unrelated", radio="something else")
    page = _FakeMenuPage(compact=False, extra_menus=[unrelated])
    submenu = await _open_chip_submenu(page, MODEL_ROW)
    assert await submenu.evaluate("") == "GPT-5.6 Sol"


async def test_waits_for_a_delayed_submenu_instead_of_grabbing_another_menu():
    # The submenu mounts a few polls late while the main menu is already there.
    page = _FakeMenuPage(compact=False, opens_after=3)
    submenu = await _open_chip_submenu(page, MODEL_ROW)
    assert await submenu.evaluate("") == "GPT-5.6 Sol"


async def test_stale_menu_plus_delayed_mount_does_not_resolve_the_stale_one():
    # THE discriminating case, and the only one where the old global
    # `count() >= 2` + `.last` resolution is actually exploitable. Radix portals
    # append, so `.last` is usually the newest menu and looks correct — until a
    # stale menu is still attached AND the real submenu has not mounted yet.
    # Then the count threshold is already satisfied on the first poll and `.last`
    # is the STALE menu, whose checked radio is the effort tier "Pro" — read as
    # a model name that is `menu_mismatch`, a fatal verdict on a healthy run.
    stale = _Menu("menu-effort-stale", state="closed", radio="Pro")
    page = _FakeMenuPage(compact=False, extra_menus=[stale], opens_after=3)
    submenu = await _open_chip_submenu(page, MODEL_ROW)
    assert await submenu.evaluate("") == "GPT-5.6 Sol"


async def test_falls_back_to_the_freshly_opened_menu_without_aria_controls():
    # aria-controls is a Radix implementation detail. If it is ever dropped, the
    # "menu that opened as a result of this click" fallback still excludes the
    # main menu and any stale one — so a rename degrades, it does not brick.
    stale = _Menu("menu-stale", state="closed", radio="Pro")
    page = _FakeMenuPage(compact=False, sets_controls=False, extra_menus=[stale])
    submenu = await _open_chip_submenu(page, MODEL_ROW)
    assert await submenu.evaluate("") == "GPT-5.6 Sol"


async def test_does_not_collapse_a_row_that_is_already_expanded():
    # Re-entrancy: clicking an expanded row toggles it SHUT. Read its owned menu
    # instead of clicking. (This is the state a call entering with menus already
    # open lands in.)
    page = _FakeMenuPage(compact=False)
    page.rows[0].expanded = True
    page.menus.append(_Menu("menu-model", radio="GPT-5.6 Sol"))
    submenu = await _open_chip_submenu(page, MODEL_ROW)
    assert page.clicks == []  # never clicked -> never collapsed
    assert await submenu.evaluate("") == "GPT-5.6 Sol"


async def test_raises_when_the_submenu_never_opens():
    # Fail closed rather than return an arbitrary menu.
    page = _FakeMenuPage(compact=False, opens_after=None)
    with pytest.raises(RuntimeError, match="did not open"):
        await _open_chip_submenu(page, MODEL_ROW, timeout=0.05)


async def test_expansion_failure_is_best_effort():
    # Navigation is forgiving, verification is not: an "Advanced" toggle that
    # really RAISES must not abort, as long as the rows are reachable anyway.
    # (The toggle is present and raising — not absent — so the swallow is what
    # is under test; deleting the try/except fails this.)
    page = _FakeMenuPage(compact=True, expand_raises=True)
    page.rows_reachable_while_compact = True
    original = page.locator

    def _locator(selector):
        if 'aria-haspopup="menu"' in selector and ADVANCED_EXPAND_LABEL not in selector:
            return _RowLocator(page, page.rows)  # reachable despite compact
        return original(selector)

    page.locator = _locator
    submenu = await _open_chip_submenu(page, MODEL_ROW)
    assert ADVANCED_EXPAND_LABEL in page.clicks  # the raising click was attempted
    assert await submenu.evaluate("") == "GPT-5.6 Sol"


# ---- read_selected_model --------------------------------------------------

async def test_read_selected_model_reads_the_submenu_radio():
    assert await read_selected_model(_FakeMenuPage(compact=True), timeout=1.0) == "GPT-5.6 Sol"


async def test_read_selected_model_returns_none_when_submenu_never_opens():
    # Degrades to None ("unknown" in doctor) rather than reading another menu.
    page = _FakeMenuPage(compact=True, opens_after=None)
    assert await read_selected_model(page, timeout=0.05) is None


async def test_read_selected_model_never_reports_the_main_menu_contents():
    # THE fail-open guard. The real main menu has a slider and no checked radio,
    # so resolving it yields None -> unverified_missing_slug (fail-OPEN). Here
    # the main menu is given a checked radio anyway: if the resolution ever
    # falls back to it, this returns a bogus model instead of failing.
    page = _FakeMenuPage(compact=True, opens_after=None, main_radio="Pro")
    assert await read_selected_model(page, timeout=0.05) is None


async def test_read_selected_model_does_not_toggle_an_already_open_chip_menu():
    # Live repro: called straight after ensure_pro_chip — which returns with its
    # menu still open — a blind chip.click() CLOSED the menu, leaving only
    # exit-animating menus, and the read degraded to None. None is the fail-OPEN
    # `unverified_missing_slug` verdict, so it weakens the missing-slug backstop
    # silently. Production does not hit this ordering today (the composer click
    # between them dismisses the menu); this pins that we don't rely on that.
    page = _FakeMenuPage(compact=False, chip_open=True)
    page.rows[0].expanded = True
    page.menus.append(_Menu("menu-model", radio="GPT-5.6 Sol"))
    assert await read_selected_model(page, timeout=1.0) == "GPT-5.6 Sol"
    assert "chip" not in page.clicks  # never toggled it shut


async def test_read_selected_model_ignores_a_stale_effort_submenu():
    # A stale effort submenu would read "Pro" as a MODEL -> classify_served_audit
    # returns menu_mismatch, killing a healthy run.
    stale = _Menu("menu-effort-stale", state="closed", radio="Pro")
    page = _FakeMenuPage(compact=True, extra_menus=[stale])
    assert await read_selected_model(page, timeout=1.0) == "GPT-5.6 Sol"
