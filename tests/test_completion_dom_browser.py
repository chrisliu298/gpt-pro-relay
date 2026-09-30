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
        context = await browser.new_context()
        page = await context.new_page()
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


async def test_authenticated_shell_with_missing_model_chip_is_not_ready(page):
    # Observed account-2 failure: home shell + composer mounted before the model
    # control. Authentication alone is insufficient to begin selecting/pasting.
    await page.set_content('''
        <button aria-label="Open profile menu"></button>
        <div contenteditable="true" role="textbox" aria-label="Ask ChatGPT"></div>
    ''')
    assert await cli.wait_for_composer_ready(page, timeout=0.05) is False


async def test_ready_requires_visible_composer_and_model_chip(page):
    await page.set_content('''
        <div contenteditable="true" role="textbox" style="min-height:20px"> </div>
        <button aria-label="Select ChatGPT model" aria-haspopup="menu">Pro</button>
    ''')
    assert await cli.wait_for_composer_ready(page, timeout=0.1) is True
    await page.locator('button').evaluate('(el) => el.style.display="none"')
    assert await cli.wait_for_composer_ready(page, timeout=0.05) is False


async def test_hidden_stop_control_does_not_prove_generation(page):
    await page.set_content('<button aria-label="Stop" style="display:none"></button>')
    assert await cli._stop_button_count(page) == 0


@pytest.mark.parametrize("sibling", [False, True])
async def test_recovered_collector_publishes_original_answer_and_closes_only_when_idle(page, tmp_path, monkeypatch, sibling):
    import json

    rd = tmp_path / "runs" / "r1"
    rd.mkdir(parents=True)
    (rd / "meta.json").write_text('{"account":3}')
    (rd / "conversation.json").write_text('{"url":"https://chatgpt.com/c/owned"}')
    monkeypatch.setattr(cli, "RUNS", rd.parent)
    monkeypatch.setattr(cli, "CLAIMS", tmp_path / "claims")
    monkeypatch.setattr(cli, "CHROME_ACTIVITY_LOCK", tmp_path / "activity.lock")
    monkeypatch.setattr(cli, "LAUNCH_LOCK", tmp_path / "launch.lock")
    monkeypatch.setattr(cli, "SLOT_LOCK_DIR", tmp_path / "slots")
    monkeypatch.setattr(cli, "configure_account", lambda n: None)
    monkeypatch.setattr(cli, "ensure_shared_chrome_running", lambda **kw: None)
    monkeypatch.setattr(cli, "COMPLETION_STABLE_SECS", 0)
    monkeypatch.setattr(cli, "_browser_run", lambda *a: pytest.fail("recovery cannot submit"))
    killed = []
    monkeypatch.setattr(cli, "_kill_chrome_orphans", lambda: killed.append(True))
    requests = []

    async def route(request_route):
        req = request_route.request
        requests.append((req.method, req.url))
        assert req.method == "GET" and req.url == "https://chatgpt.com/c/owned"
        await request_route.fulfill(content_type="text/html", body='''
            <article data-testid="conversation-turn-2">
              <div data-message-author-role="assistant" data-message-model-slug="gpt-6-pro">Original answer</div>
              <button data-testid="copy-turn-action-button"
                onclick="navigator.clipboard.writeText('# Original answer\\n\\nComplete markdown.')">Copy</button>
            </article>
        ''')

    await page.context.route("**/*", route)

    async def connect(pw):
        return page.context

    async def logged_in(ctx):
        return True

    monkeypatch.setattr(cli, "connect_shared_chrome", connect)
    monkeypatch.setattr(cli, "is_logged_in", logged_in)
    holder = cli.ChromeActivityLease() if sibling else None
    if holder:
        holder.acquire()
    try:
        from types import SimpleNamespace
        assert await cli.cmd_run(SimpleNamespace(run_id="r1"), recover=True) == 0
    finally:
        if holder:
            holder.release()
    result = json.loads((rd / "result.json").read_text())
    assert result["status"] == "ok" and result["account"] == 3
    assert result["model_audit"] == "verified"
    assert (rd / "response.md").read_text() == "# Original answer\n\nComplete markdown."
    assert requests == [("GET", "https://chatgpt.com/c/owned")]
    assert killed == ([] if sibling else [True])
    assert len(page.context.pages) == 1  # collector's page closed, sibling intact


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


async def test_current_turn_toolbar_completes_without_rate_response_button(page):
    await page.set_content(
        """
        <div data-turn-key="turn-1">
          <span data-chatgpt-agent-turn-start></span>
          <div class="answer"><pre><button aria-label="Copy">code copy</button></pre></div>
          <div class="turn-action-controls">
            <button aria-label="Copy">response copy</button>
            <button aria-label="Share"></button>
            <button aria-label="Regenerate response"></button>
            <button aria-label="React"></button>
          </div>
        </div>
        """
    )

    assert await cli._copy_button_present(page) is True
