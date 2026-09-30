"""The relay must never take the macOS foreground from the human at the keyboard.

Measured live 2026-09-29 (account 3, another app frontmost throughout): with the
window created in the background, the chip slider, an element-dispatched paste
(inline and the large-paste attachment path), and in-page Copy capture all work
with no activation — Playwright's CDP focus emulation makes the target tab
behave as focused. Only interactive `login` still activates Chrome. Neither
paste nor extraction touches the system pasteboard.
"""
import pytest

from gpt_pro import cli


def _forbidden_activation(*_a, **_k):
    raise AssertionError("worker path must not activate Chrome")


class _CopyPage:
    def __init__(self, captured):
        self.captured = captured

    async def evaluate(self, _js):
        return self.captured

    def is_closed(self):
        return False


@pytest.fixture
def _no_pasteboard(monkeypatch):
    def _forbidden(*_a, **_k):
        raise AssertionError("copy extraction must not activate Chrome or touch the pasteboard")

    monkeypatch.setattr(cli, "bind_chrome_compositor_surface", _forbidden)
    monkeypatch.setattr(cli, "bring_tab_to_front", _forbidden)
    monkeypatch.setattr(cli.subprocess, "run", _forbidden)


async def test_copy_extraction_captures_in_page(_no_pasteboard):
    assert await cli._copy_button_extract(_CopyPage("the copied answer")) == "the copied answer"


@pytest.mark.parametrize("captured", [None, "", "  \n"])
async def test_copy_extraction_without_capture_falls_back(_no_pasteboard, captured):
    assert await cli._copy_button_extract(_CopyPage(captured)) is None


class _Page:
    def __init__(self, url):
        self.url = url


class _Cdp:
    def __init__(self, sent):
        self.sent = sent

    async def send(self, method, params=None):
        self.sent.append((method, params))
        return {"targetId": "t"}

    async def detach(self):
        return None


class _Browser:
    def __init__(self):
        self.sent = []

    async def new_browser_cdp_session(self):
        return _Cdp(self.sent)


class _Ctx:
    def __init__(self, urls):
        self.pages = [_Page(u) for u in urls]


@pytest.mark.parametrize("urls", [[], ["chrome://omnibox-popup.top-chrome/"]])
async def test_windowless_chrome_gets_a_background_window(urls):
    # `--no-startup-window` launches Chrome with no window; the first
    # `ctx.new_page()` would open one in the foreground, so create it via CDP
    # with background=True first. WebUI-only targets are not a real window.
    browser = _Browser()
    await cli._ensure_background_window(browser, _Ctx(urls))
    assert browser.sent == [(
        "Target.createTarget",
        {"url": "about:blank", "newWindow": True, "background": True},
    )]


async def test_existing_window_is_reused():
    browser = _Browser()
    await cli._ensure_background_window(browser, _Ctx(["https://chatgpt.com/"]))
    assert browser.sent == []


async def test_window_creation_failure_is_fail_open(monkeypatch):
    stages = []
    monkeypatch.setattr(cli, "log_stage", lambda stage, **kw: stages.append(stage))

    class _Broken:
        async def new_browser_cdp_session(self):
            raise RuntimeError("cdp gone")

    await cli._ensure_background_window(_Broken(), _Ctx([]))
    assert stages == ["chrome_background_window_skipped"]
