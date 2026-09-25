import pytest

from gpt_pro import cli


class _Context:
    def __init__(self, cookie_names):
        self.cookie_names = cookie_names

    async def cookies(self, _url):
        return [{"name": name} for name in self.cookie_names]


class _Locator:
    def __init__(self, count):
        self._count = count

    async def count(self):
        return self._count


class _Page:
    def __init__(self, profile_buttons):
        self.profile_buttons = profile_buttons
        self.selectors = []

    def locator(self, selector):
        self.selectors.append(selector)
        return _Locator(self.profile_buttons)


@pytest.mark.asyncio
async def test_login_completion_rejects_intermediate_auth_cookie():
    ctx = _Context(["__Secure-next-auth.session-token.0"])
    page = _Page(profile_buttons=0)

    assert await cli.is_login_complete(ctx, page) is False
    assert page.selectors == [cli.AUTHENTICATED_SHELL_SELECTOR]


@pytest.mark.asyncio
async def test_login_completion_requires_cookie_and_authenticated_app_shell():
    ctx = _Context(["__Secure-next-auth.session-token.0"])
    page = _Page(profile_buttons=1)

    assert await cli.is_login_complete(ctx, page) is True
    assert 'button[aria-label="Open profile menu"]' in cli.AUTHENTICATED_SHELL_SELECTOR


@pytest.mark.asyncio
async def test_login_completion_rejects_shell_without_session_cookie():
    ctx = _Context([])
    page = _Page(profile_buttons=1)

    assert await cli.is_login_complete(ctx, page) is False
    assert page.selectors == []


def test_home_composer_redesign_selectors_are_supported():
    assert 'button[aria-label="Select ChatGPT model"]' in cli.COMPOSER_CHIP
    assert 'button[aria-label="Send"]' in cli.SEND_BUTTON
    assert 'button[aria-label="Send"]' in cli.SEND_BUTTON_READY
