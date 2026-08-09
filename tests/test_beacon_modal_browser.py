"""Real-Playwright proof that the beacon dismisser actually fires.

The fakes in `test_rate_limit_modal.py` pin construction and hand-invocation of
the handler; they cannot prove the parts that live in Playwright — that
`add_locator_handler` accepts a chained `.first` locator, that the handler is
triggered by the *visibility* of the close button, and that dismissing it
actually unblocks the click the overlay was intercepting. That gap is what let
the first version of this fix ship a docstring claim (stacked beacons clear one
at a time) that a real browser immediately disproved.

Opt-in — this is the only browser-dependent test in the suite, and the default
`pytest tests/` must stay a ~1.5s pure-fake run:

    GPT_PRO_BROWSER_TESTS=1 .venv/bin/python -m pytest tests/test_beacon_modal_browser.py

The fixture mirrors the DOM captured in `ask-20260808T010319Z-d6177828`'s
`error.html` — `#modal-beacon` > overlay > `role=dialog` > (`close-button`,
"Get started") — with the overlay geometry inlined, because the real capture's
`fixed inset-0 z-50` classes live in a CDN stylesheet that will not load offline.
Everything the assertions depend on (test-ids, nesting, `pointer-events: auto`)
is copied from the capture, not invented.
"""

import os
import pathlib

import pytest

from gpt_pro import cli

pytestmark = pytest.mark.skipif(
    os.environ.get("GPT_PRO_BROWSER_TESTS") != "1",
    reason="browser test; set GPT_PRO_BROWSER_TESTS=1 to run",
)

OVERLAY_STYLE = (
    "position:fixed;inset:0;z-index:50;background:rgba(0,0,0,.4);pointer-events:auto;"
)

BEACON = f"""
<div id="modal-beacon" data-testid="modal-beacon" data-ignore-for-page-load="true">
  <div data-state="open" style="{OVERLAY_STYLE}">
    <div role="dialog" data-state="open" style="pointer-events:auto;">
      <button data-testid="close-button" aria-label="Close"
              onclick="this.closest('#modal-beacon').remove();window.__closed=true">x</button>
      <div>You now have access to Health in ChatGPT</div>
      <button onclick="window.__cta=true">Get started</button>
    </div>
  </div>
</div>"""


def _page_html(with_beacon: bool) -> str:
    return (
        "<!doctype html><html><body>"
        '<button data-testid="send-button" aria-label="Send prompt" '
        'onclick="window.__sent=true">Send</button>'
        + (BEACON if with_beacon else "")
        + "</body></html>"
    )


def _browser_kwargs():
    """Bundled-browser path, or a cached build if the pin is mismatched.

    This repo drives real Chrome over CDP and never runs `playwright install`, so
    the bundled download for the pinned version is usually absent. Fall back to
    any cached chromium build rather than making the test's usefulness depend on
    a download the project otherwise doesn't need.
    """
    cache = pathlib.Path.home() / "Library" / "Caches" / "ms-playwright"
    for exe in sorted(cache.glob("chromium-*/chrome-mac-arm64/*.app/Contents/MacOS/*")):
        if exe.is_file() and os.access(exe, os.X_OK):
            return {"executable_path": str(exe)}
    return {}


@pytest.fixture
async def _browser():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(**_browser_kwargs())
        except Exception as e:  # no usable browser on this machine
            pytest.skip(f"no launchable chromium: {type(e).__name__}: {e}")
        yield browser
        await browser.close()


async def _open(browser, tmp_path, *, with_beacon):
    f = tmp_path / f"beacon_{with_beacon}.html"
    f.write_text(_page_html(with_beacon))
    page = await (await browser.new_context()).new_page()
    await page.goto(f.as_uri())
    return page


async def test_beacon_overlay_blocks_the_send_click_without_the_handler(_browser, tmp_path):
    # The bug itself: this is what killed ask-20260808T010319Z-d6177828. If this
    # ever stops failing, the fixture no longer reproduces the hazard and the
    # test below proves nothing.
    page = await _open(_browser, tmp_path, with_beacon=True)
    with pytest.raises(Exception) as excinfo:
        await page.get_by_test_id("send-button").click(timeout=3000)
    assert "intercepts pointer events" in str(excinfo.value)
    assert await page.evaluate("window.__sent===true") is False


async def test_handler_dismisses_the_beacon_and_the_send_click_lands(_browser, tmp_path, monkeypatch):
    stages = []
    monkeypatch.setattr(cli, "log_stage", lambda stage, **kw: stages.append(stage))

    page = await _open(_browser, tmp_path, with_beacon=True)
    await cli._install_modal_dismissers(page)
    # Same click that fails above — the handler must fire inside its actionability
    # wait, clear the overlay, and let it through.
    await page.get_by_test_id("send-button").click(timeout=10000)

    assert await page.evaluate("window.__sent===true") is True
    assert await page.evaluate("window.__closed===true") is True
    # Close, never the CTA: "Get started" also dismisses the dialog but opts the
    # account into the announced feature.
    assert await page.evaluate("!!window.__cta") is False
    assert "beacon_modal_dismissed" in stages


async def test_handler_stays_silent_when_no_beacon_is_present(_browser, tmp_path, monkeypatch):
    stages = []
    monkeypatch.setattr(cli, "log_stage", lambda stage, **kw: stages.append(stage))

    page = await _open(_browser, tmp_path, with_beacon=False)
    await cli._install_modal_dismissers(page)
    await page.get_by_test_id("send-button").click(timeout=5000)

    assert await page.evaluate("window.__sent===true") is True
    assert stages == []  # no spurious dismissals on a clean page
