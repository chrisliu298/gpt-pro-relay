"""Auto-dismissal of the blocking full-viewport modals ChatGPT renders over the composer.

Two distinct modals, same blast radius. Both mount a `fixed inset-0 z-50`
overlay with `pointer-events: auto`, which makes the composer/send button
un-clickable, so a worker that reaches a click while one is up hangs the full 30s
actionability timeout and dies `worker_exception`:

  - **"Too many requests"** (`data-testid=modal-conversation-history-rate-limit`,
    dismiss button "Got it"). A parallel burst throttles the sidebar
    conversation-list endpoint (`/backend-api/conversations` → 429). Observed
    2026-07-19, 2 runs.
  - **The product-announcement "beacon"** (`data-testid=modal-beacon`, dismissed
    via its own `data-testid=close-button`). Observed 2026-08-08, run
    `ask-20260808T010319Z-d6177828` died on the *send* click — past paste and
    both chip verifications — against "You now have access to Health in
    ChatGPT". The rate-limit handler is inert against it (different test-id),
    which is exactly why each modal needs its own handler.

`_install_modal_dismissers` registers a Playwright locator handler per modal,
which fires before the actionability checks of every downstream click. These pin:
each handler is registered against the right trigger, clicking targets the
dismiss affordance (never a modal's primary CTA) and emits the observability
stage, both are installed together so a recovery site cannot get only one, and a
registration failure (fake page in tests, or a selector rename in prod) never
aborts the run — these are UI-overlay cleaners, not rate-limit backoff.
"""

import pytest

from gpt_pro import cli
from gpt_pro.cli import (
    BEACON_MODAL_CLOSE_TESTID,
    BEACON_MODAL_TESTID,
    RATE_LIMIT_MODAL_TESTID,
    _install_beacon_modal_dismisser,
    _install_modal_dismissers,
    _install_rate_limit_dismisser,
)


class _FakeButton:
    def __init__(self, rec):
        self._rec = rec

    async def click(self, timeout=None):
        self._rec["clicked"] = True
        self._rec["timeout"] = timeout


class _FakeModalLocator:
    def __init__(self, rec):
        self._rec = rec

    def get_by_role(self, role, name=None):
        self._rec["role"] = role
        self._rec["name"] = name
        return _FakeButton(self._rec)

    def get_by_test_id(self, testid):
        # Nested scoping (beacon container → its own close button). Recorded
        # separately from the page-level chain so a test can prove the close
        # button is resolved *inside* the beacon rather than page-wide.
        self._rec.setdefault("nested_testids", []).append(testid)
        return _FakeModalLocator(self._rec)

    @property
    def first(self):
        self._rec["first"] = True
        return self

    async def click(self, timeout=None):
        self._rec["clicked"] = True
        self._rec["timeout"] = timeout


class _FakePage:
    """Records handler registration + the button the handler resolves."""

    def __init__(self, *, raise_on_register=False):
        self.handlers = []  # [(trigger_locator, handler)], in registration order
        self.testids = []
        self.rec = {}
        self._raise = raise_on_register

    @property
    def registered(self):
        return self.handlers[-1] if self.handlers else None

    def get_by_test_id(self, testid):
        self.testids.append(testid)
        return _FakeModalLocator(self.rec)

    async def add_locator_handler(self, locator, handler, **_kw):
        if self._raise:
            raise RuntimeError("no locator-handler support")
        self.handlers.append((locator, handler))


@pytest.fixture
def _stages(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "log_stage", lambda stage, **kw: seen.append((stage, kw)))
    return seen


async def test_registers_handler_against_the_rate_limit_modal(_stages):
    page = _FakePage()
    await _install_rate_limit_dismisser(page)
    assert page.registered is not None
    # The trigger locator resolves the conversation-history rate-limit modal.
    assert RATE_LIMIT_MODAL_TESTID in page.testids


async def test_handler_clicks_got_it_and_logs(_stages):
    page = _FakePage()
    await _install_rate_limit_dismisser(page)
    _locator, handler = page.registered
    await handler()
    assert page.rec.get("clicked") is True
    assert page.rec.get("role") == "button"
    assert page.rec.get("name") == "Got it"
    assert page.rec.get("timeout") == 5000
    assert ("rate_limit_modal_dismissed", {}) in _stages


# ---- the product-announcement beacon modal ----


async def test_beacon_handler_triggers_on_the_close_button_not_the_container(_stages):
    page = _FakePage()
    await _install_beacon_modal_dismisser(page)
    assert page.registered is not None
    # Scoped: the close button is resolved *inside* the beacon container, so a
    # same-test-id close button belonging to some other dialog is out of reach.
    assert page.testids == [BEACON_MODAL_TESTID]
    assert page.rec.get("nested_testids") == [BEACON_MODAL_CLOSE_TESTID]
    # `.first` is a strict-mode guard: a second match must not raise *inside* the
    # handler, because a locator-handler exception propagates into whatever
    # action triggered it — turning a fail-open cleaner into a send failure.
    assert page.rec.get("first") is True


async def test_beacon_handler_clicks_close_and_logs(_stages):
    page = _FakePage()
    await _install_beacon_modal_dismisser(page)
    _locator, handler = page.registered
    await handler()
    assert page.rec.get("clicked") is True
    assert page.rec.get("timeout") == 5000
    # The handler clicks the very element that triggered it — it does not build a
    # second locator (which could resolve a *different* button than the one
    # Playwright verified as visible).
    assert page.rec.get("nested_testids") == [BEACON_MODAL_CLOSE_TESTID]
    # Never the modal's primary CTA. "Get started" opts the account into the
    # announced feature; dismissal must be inert.
    assert page.rec.get("name") is None
    assert ("beacon_modal_dismissed", {}) in _stages


async def test_beacon_registration_failure_never_aborts_the_run(_stages):
    page = _FakePage(raise_on_register=True)
    await _install_beacon_modal_dismisser(page)  # must not raise
    assert page.handlers == []
    assert any(s == "beacon_dismisser_install_skipped" for s, _ in _stages)


# ---- both dismissers install together ----


async def test_install_modal_dismissers_registers_both(_stages):
    # Single entry point so a future page-recovery site cannot pick up one
    # dismisser and silently miss the other.
    page = _FakePage()
    await _install_modal_dismissers(page)
    assert len(page.handlers) == 2
    assert page.testids == [RATE_LIMIT_MODAL_TESTID, BEACON_MODAL_TESTID]


async def test_install_modal_dismissers_survives_a_failing_registration(_stages):
    page = _FakePage(raise_on_register=True)
    await _install_modal_dismissers(page)  # must not raise
    skipped = {s for s, _ in _stages}
    assert "rate_limit_dismisser_install_skipped" in skipped
    # Both are attempted: the first one failing must not skip the second.
    assert "beacon_dismisser_install_skipped" in skipped


# ---- _focus_and_paste is element-bound and never touches the pasteboard ----
#
# The dismisser is a locator handler: it only fires at an action's actionability
# checkpoint, so `_focus_and_paste` first clicks the composer *locator* (handler
# checkpoint + focus). The paste itself is a ClipboardEvent dispatched on the
# composer element, so a modal mounting afterwards cannot redirect it the way it
# could steal a focus-following key press, and a page-level keyboard is never
# used. No pbcopy/pbpaste: the human's clipboard is never read or written.

class _RecordingComposer:
    def __init__(self, handled=True):
        self.clicked = False
        self.pressed = []
        self.evaluated = []
        self.handled = handled

    async def click(self):
        self.clicked = True

    async def press(self, key, **_kw):
        self.pressed.append(key)

    async def evaluate(self, js, arg=None):
        self.evaluated.append((js, arg))
        return self.handled


class _RecordingKeyboard:
    def __init__(self):
        self.pressed = []

    async def press(self, key, **_kw):
        self.pressed.append(key)


class _PastePage:
    def __init__(self):
        self.keyboard = _RecordingKeyboard()


@pytest.fixture
def _paste_env(monkeypatch, _stages):
    def _forbidden(*_a, **_k):
        raise AssertionError("paste must not activate Chrome or touch the pasteboard")

    monkeypatch.setattr(cli, "bind_chrome_compositor_surface", _forbidden)
    monkeypatch.setattr(cli, "bring_tab_to_front", _forbidden)
    monkeypatch.setattr(cli.subprocess, "run", _forbidden)
    return monkeypatch


async def test_focus_and_paste_dispatches_on_the_composer_element(_paste_env):
    page = _PastePage()
    composer = _RecordingComposer()
    await cli._focus_and_paste(page, composer, "the prompt body")
    assert composer.clicked is True
    assert [arg for _js, arg in composer.evaluated] == ["the prompt body"]
    assert "ClipboardEvent('paste'" in composer.evaluated[0][0]
    assert composer.pressed == []
    assert page.keyboard.pressed == []


async def test_unhandled_paste_fails_closed_before_send(_paste_env):
    # A synthetic paste has no native default action: if no handler consumed it,
    # nothing reached the composer. Fail now rather than wait out the upload gate.
    with pytest.raises(RuntimeError, match="paste_not_handled"):
        await cli._focus_and_paste(_PastePage(), _RecordingComposer(handled=False), "x")


async def test_registration_failure_never_aborts_the_run(_stages):
    # A genuine add_locator_handler API/channel failure (unhealthy CDP, an
    # incompatible Playwright) must be swallowed — the dismisser is best-effort
    # and must never brick a real send. It logs a skip breadcrumb instead. NOTE
    # this is NOT the selector-rename case: Playwright locators are lazy, so a
    # renamed test-id does not raise here at all — registration succeeds and the
    # handler simply never fires (covered by leaving the handler inert, not by
    # this except path).
    page = _FakePage(raise_on_register=True)
    await _install_rate_limit_dismisser(page)  # must not raise
    assert page.registered is None
    assert any(s == "rate_limit_dismisser_install_skipped" for s, _ in _stages)
