"""Real-browser coverage for old and 2026-09 conversation DOM shapes."""

import os
import pathlib

import pytest

from gpt_pro import cli

pytestmark = pytest.mark.skipif(
    os.environ.get("GPT_PRO_BROWSER_TESTS") != "1",
    reason="browser test; set GPT_PRO_BROWSER_TESTS=1 to run",
)


def _browser_kwargs():
    cache = pathlib.Path.home() / "Library" / "Caches" / "ms-playwright"
    for exe in sorted(cache.glob("chromium-*/chrome-mac-arm64/*.app/Contents/MacOS/*")):
        if exe.is_file() and os.access(exe, os.X_OK):
            return {"executable_path": str(exe)}
    chrome_beta = pathlib.Path(
        "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"
    )
    if chrome_beta.is_file():
        return {"executable_path": str(chrome_beta)}
    return {}


@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(**_browser_kwargs())
        except Exception as e:
            pytest.skip(f"no launchable chromium: {type(e).__name__}: {e}")
        page = await browser.new_page()
        yield page
        await browser.close()


async def test_legacy_assistant_dom_still_completes(page):
    await page.set_content(
        """
        <article data-testid="conversation-turn-2">
          <div data-message-author-role="assistant" data-message-model-slug="gpt-6-pro">
            legacy answer
          </div>
          <button data-testid="copy-turn-action-button">Copy</button>
        </article>
        """
    )

    assert (await cli.read_latest_assistant_text(page)).strip() == "legacy answer"
    assert await cli._copy_button_present(page) is True
    assert await cli.served_assistant_model_slug(page) == "gpt-6-pro"


async def test_new_turn_dom_finds_answer_and_response_copy_not_code_copy(page):
    await page.set_content(
        """
        <div data-turn-key="turn-1">
          <div data-user-message-bubble="true">private user prompt</div>
          <span data-chatgpt-agent-turn-start></span>
          <div>Worked for 12m</div>
          <div class="answer">
            final answer
            <pre><button aria-label="Copy">code copy</button></pre>
          </div>
          <div class="turn-actions">
            <button aria-label="Copy">response copy</button>
            <button aria-label="Rate response"></button>
            <button aria-label="Regenerate response"></button>
          </div>
        </div>
        """
    )

    text = await cli.read_latest_assistant_text(page)
    assert "final answer" in text
    assert "private user prompt" not in text
    assert await cli._copy_button_present(page) is True
    assert await cli._user_turn_present(page) is True


async def test_new_turn_code_copy_alone_is_not_completion(page):
    await page.set_content(
        """
        <div data-turn-key="turn-1">
          <div data-user-message-bubble="true">prompt</div>
          <span data-chatgpt-agent-turn-start></span>
          <pre><button aria-label="Copy">code copy</button></pre>
        </div>
        """
    )

    assert await cli._copy_button_present(page) is False
