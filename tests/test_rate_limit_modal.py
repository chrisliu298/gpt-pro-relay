"""Auto-dismissal of the "Too many requests" conversation-history rate-limit modal.

During a parallel burst ChatGPT throttles the sidebar conversation-list endpoint
(`/backend-api/conversations` → 429) and renders a full-viewport `fixed inset-0
z-50` modal (`data-testid=modal-conversation-history-rate-limit`, dismiss button
"Got it"). Its `pointer-events: auto` backdrop makes the composer un-clickable,
so a worker that reaches `composer.click()` while it is up hangs the full 30s
actionability timeout and dies `worker_exception` (observed 2026-07-19, 2 runs).

`_install_rate_limit_dismisser` registers a Playwright locator handler that
clicks "Got it" before the actionability checks of every downstream click. These
pin: the handler is registered against the right modal, clicking targets the
"Got it" button and emits the observability stage, and a registration failure
(fake page in tests, or a selector rename in prod) never aborts the run — it is a
UI-overlay cleaner, not rate-limit backoff.
"""

import pytest

from gpt_pro import cli
from gpt_pro.cli import RATE_LIMIT_MODAL_TESTID, _install_rate_limit_dismisser


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


class _FakePage:
    """Records handler registration + the button the handler resolves."""

    def __init__(self, *, raise_on_register=False):
        self.registered = None  # (trigger_locator, handler)
        self.testids = []
        self.rec = {}
        self._raise = raise_on_register

    def get_by_test_id(self, testid):
        self.testids.append(testid)
        return _FakeModalLocator(self.rec)

    async def add_locator_handler(self, locator, handler, **_kw):
        if self._raise:
            raise RuntimeError("no locator-handler support")
        self.registered = (locator, handler)


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


# ---- _focus_and_paste is locator-bound (the dismisser's focus guarantee) ----
#
# The dismisser is a locator handler: it only fires at an action's actionability
# checkpoint. A bare `page.keyboard.press("Meta+V")` has no such checkpoint, so a
# modal that mounts in the gap between composer.click() and the paste would steal
# focus and the paste would land in the modal. `_focus_and_paste` must therefore
# paste via the composer *locator* (`composer.press`), which re-runs the handler
# checkpoint and re-focuses the composer immediately before dispatching the keys.

class _RecordingComposer:
    def __init__(self):
        self.clicked = False
        self.pressed = []

    async def click(self):
        self.clicked = True

    async def press(self, key, **_kw):
        self.pressed.append(key)


class _RecordingKeyboard:
    def __init__(self):
        self.pressed = []

    async def press(self, key, **_kw):
        self.pressed.append(key)


class _PastePage:
    def __init__(self):
        self.keyboard = _RecordingKeyboard()

    async def wait_for_selector(self, *_a, **_k):
        return object()  # send button "mounted" → paste settled


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _FakeCompleted:
    stdout = ""


@pytest.fixture
def _paste_env(monkeypatch, _stages):
    async def _noop_async(*_a, **_k):
        return None

    monkeypatch.setattr(cli, "UiClipboardLock", _NoLock)
    monkeypatch.setattr(cli, "bind_chrome_compositor_surface", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "bring_tab_to_front", _noop_async)
    monkeypatch.setattr(cli.subprocess, "run", lambda *_a, **_k: _FakeCompleted())
    return monkeypatch


async def test_focus_and_paste_uses_locator_press_not_bare_keyboard(_paste_env):
    page = _PastePage()
    composer = _RecordingComposer()
    await cli._focus_and_paste(page, composer, "the prompt body")
    # The paste is dispatched through the composer locator (handler checkpoint +
    # focus), never the un-checkpointed page-level keyboard.
    assert composer.pressed == ["Meta+V"]
    assert page.keyboard.pressed == []
    assert composer.clicked is True


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
