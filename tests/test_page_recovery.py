"""Tests for post-send page/tab-close recovery.

When a user closes this worker's Chrome tab mid-run, the worker must reopen the
SAME captured conversation on a fresh tab and resume monitoring — never
re-pasting or re-sending (a resend burns 5-20 min of Pro reasoning). These pin
the safety properties: closure is detected (not swallowed as empty text or a
fail-open audit), the conversation URL is validated before it is trusted,
recovery is bounded, the original generation deadline is preserved, and a
non-closure failure is never converted into a recovery.

Whether ChatGPT actually resumes DOM streaming after reopening a mid-flight
conversation is server/UI behavior that fakes cannot prove — that is the
explicitly-authorized live test, not these units.
"""

import inspect
import json

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from gpt_pro import cli
from gpt_pro.cli import (
    RunPageClosed,
    _ConversationUrl,
    _confirm_send_landed,
    _monitor_and_finalize,
    _recover_navigate,
    _stop_button_count,
    classify_recovery,
    parse_conversation_url,
    read_latest_assistant_text,
)


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    async def _noop(_delay):
        return None

    monkeypatch.setattr(cli.asyncio, "sleep", _noop)


# ---- parse_conversation_url ----

def test_parse_conversation_url_accepts_canonical():
    u = "https://chatgpt.com/c/6870a1b2-dead-beef-0000-111122223333"
    assert parse_conversation_url(u) == u


def test_parse_conversation_url_strips_query_and_fragment():
    base = "https://chatgpt.com/c/abc-123"
    assert parse_conversation_url(base + "?model=gpt&x=1#frag") == base


def test_parse_conversation_url_rejects_non_conversation_routes():
    for bad in (
        None,
        "",
        "https://chatgpt.com/",            # home
        "https://chatgpt.com/auth/login",  # login redirect
        "https://chatgpt.com/c/",          # empty id
        "https://chatgpt.com/c/ab/cd",     # deeper path
        "http://chatgpt.com/c/abc",        # not https
        "https://evil.com/c/abc",          # foreign host
        "https://user:pass@chatgpt.com/c/abc",  # embedded credentials
        "https://chatgpt.com:8443/c/abc",  # non-default port in netloc
        "https://chat.openai.com/c/abc",   # old host, not the pinned one
    ):
        assert parse_conversation_url(bad) is None, bad


# ---- _ConversationUrl ----

def test_conversation_url_captures_first_valid_and_is_immutable():
    conv = _ConversationUrl()
    assert conv.get() is None
    conv.capture("https://chatgpt.com/")          # home → ignored
    assert conv.get() is None
    conv.capture("https://chatgpt.com/c/first")
    assert conv.get() == "https://chatgpt.com/c/first"
    conv.capture("https://chatgpt.com/c/second")  # immutable: cannot repoint
    assert conv.get() == "https://chatgpt.com/c/first"


def test_conversation_url_persists_once(tmp_path):
    conv = _ConversationUrl()
    conv.capture("https://chatgpt.com/c/abc?ref=1")
    conv.persist(tmp_path)
    art = tmp_path / "conversation.json"
    assert art.exists()
    data = json.loads(art.read_text())
    assert data["url"] == "https://chatgpt.com/c/abc"
    assert "captured_at" in data
    # A second persist is a no-op (does not rewrite / duplicate-log).
    art.write_text("SENTINEL")
    conv.persist(tmp_path)
    assert art.read_text() == "SENTINEL"


def test_conversation_url_persist_noop_without_capture(tmp_path):
    conv = _ConversationUrl()
    conv.persist(tmp_path)
    assert not (tmp_path / "conversation.json").exists()


# ---- classify_recovery ----

def test_classify_recovery_no_url_is_terminal_even_with_budget():
    # No captured URL → never guess a conversation, regardless of budget/time.
    assert classify_recovery(None, 0, 3, 999.0) == "no_url"


def test_classify_recovery_deadline_beats_budget():
    url = "https://chatgpt.com/c/abc"
    assert classify_recovery(url, 0, 3, 0.0) == "deadline"
    assert classify_recovery(url, 0, 3, -5.0) == "deadline"


def test_classify_recovery_exhausted_when_budget_spent():
    url = "https://chatgpt.com/c/abc"
    assert classify_recovery(url, 3, 3, 100.0) == "exhausted"
    assert classify_recovery(url, 4, 3, 100.0) == "exhausted"


def test_classify_recovery_recovers_with_url_time_and_budget():
    url = "https://chatgpt.com/c/abc"
    assert classify_recovery(url, 0, 3, 100.0) == "recover"
    assert classify_recovery(url, 2, 3, 100.0) == "recover"


# ---- read_latest_assistant_text / _stop_button_count: close vs live-transient ----

class _EvalPage:
    def __init__(self, *, value=None, closed=False, raise_exc=None):
        self._value = value
        self._closed = closed
        self._raise = raise_exc

    def is_closed(self):
        return self._closed

    async def evaluate(self, _js):
        if self._raise is not None:
            raise self._raise
        return self._value


async def test_read_text_returns_value():
    assert await read_latest_assistant_text(_EvalPage(value="hello")) == "hello"


async def test_read_text_live_transient_returns_empty():
    # Page still open: a transient eval failure is swallowed to "" (conservative),
    # NOT promoted to recovery — a momentary DOM hiccup must not reopen the tab.
    page = _EvalPage(closed=False, raise_exc=RuntimeError("eval glitch"))
    assert await read_latest_assistant_text(page) == ""


async def test_read_text_closed_raises_run_page_closed():
    page = _EvalPage(closed=True, raise_exc=RuntimeError("Target page, context or browser has been closed"))
    with pytest.raises(RunPageClosed):
        await read_latest_assistant_text(page)


class _StopPage:
    def __init__(self, *, count=0, closed=False, raise_exc=None):
        self._count = count
        self._closed = closed
        self._raise = raise_exc

    def is_closed(self):
        return self._closed

    def locator(self, _sel):
        return self

    async def count(self):
        if self._raise is not None:
            raise self._raise
        return self._count


async def test_stop_count_normal():
    assert await _stop_button_count(_StopPage(count=0)) == 0
    assert await _stop_button_count(_StopPage(count=2)) == 2


async def test_stop_count_live_failure_is_conservative():
    # Ambiguous read on a live page → treat as "still running" (1), never
    # false-complete the turn.
    page = _StopPage(closed=False, raise_exc=RuntimeError("glitch"))
    assert await _stop_button_count(page) == 1


async def test_stop_count_closed_raises():
    page = _StopPage(closed=True, raise_exc=RuntimeError("Target closed"))
    with pytest.raises(RunPageClosed):
        await _stop_button_count(page)


# ---- hardened audit helpers: a close raises instead of fail-open ----

async def test_served_slug_closed_raises_not_none():
    # The critical hole: a close during the audit must NOT degrade to a
    # fail-open "unverified" (slug None) that returns a wrong-model turn as ok.
    page = _EvalPage(closed=True, raise_exc=RuntimeError("Target closed"))
    with pytest.raises(RunPageClosed):
        await cli.served_assistant_model_slug(page)


async def test_served_slug_live_failure_returns_none():
    page = _EvalPage(closed=False, raise_exc=RuntimeError("glitch"))
    assert await cli.served_assistant_model_slug(page) is None


async def test_copy_present_closed_raises_not_false():
    page = _EvalPage(closed=True, raise_exc=RuntimeError("Target closed"))
    with pytest.raises(RunPageClosed):
        await cli._copy_button_present(page)


async def test_copy_present_live_failure_returns_false():
    page = _EvalPage(closed=False, raise_exc=RuntimeError("glitch"))
    assert await cli._copy_button_present(page) is False


# ---- _recover_navigate ----

class _NavPage:
    def __init__(self, *, final_url, closed=False, goto_exc=None, shell=True):
        self._final_url = final_url
        self._closed = closed
        self._goto_exc = goto_exc
        self._shell = shell
        self.goto_calls = []

    @property
    def url(self):
        return self._final_url

    def is_closed(self):
        return self._closed

    async def goto(self, url, *, wait_until, timeout):
        self.goto_calls.append({"url": url, "timeout": timeout})
        if self._goto_exc is not None:
            raise self._goto_exc

    async def wait_for_selector(self, _sel, *, timeout, state):
        if not self._shell:
            raise PlaywrightTimeoutError("no shell")


@pytest.fixture
def _nav_env(monkeypatch):
    """Neutralize the browser-touching bits of _recover_navigate: viewport pin is
    a no-op and auth is logged-in by default (override per-test)."""
    async def _pin(_ctx, _page):
        return None

    async def _logged_in(_ctx):
        return True

    monkeypatch.setattr(cli, "pin_viewport_cdp", _pin)
    monkeypatch.setattr(cli, "is_logged_in", _logged_in)
    return monkeypatch


async def _future_deadline():
    import asyncio
    return asyncio.get_running_loop().time() + 100.0


async def test_recover_navigate_success(_nav_env):
    url = "https://chatgpt.com/c/abc"
    page = _NavPage(final_url=url)
    assert await _recover_navigate(object(), page, url, deadline=await _future_deadline()) is None
    # Reopened the exact captured conversation.
    assert page.goto_calls[0]["url"] == url


async def test_recover_navigate_redirect(_nav_env):
    # Landed on home even though we asked for /c/abc → reject, never monitor a
    # different/empty conversation.
    page = _NavPage(final_url="https://chatgpt.com/")
    reason = await _recover_navigate(object(), page, "https://chatgpt.com/c/abc", deadline=await _future_deadline())
    assert reason == "redirect"


async def test_recover_navigate_auth_lost(_nav_env):
    async def _logged_out(_ctx):
        return False

    _nav_env.setattr(cli, "is_logged_in", _logged_out)
    url = "https://chatgpt.com/c/abc"
    page = _NavPage(final_url=url)
    assert await _recover_navigate(object(), page, url, deadline=await _future_deadline()) == "auth_lost"


async def test_recover_navigate_shell_missing(_nav_env):
    url = "https://chatgpt.com/c/abc"
    page = _NavPage(final_url=url, shell=False)
    assert await _recover_navigate(object(), page, url, deadline=await _future_deadline()) == "shell_missing"


async def test_recover_navigate_auth_exception_is_auth_lost(_nav_env):
    # is_logged_in raising (including a wait_for timeout on a wedged CDP session,
    # now that the call is deadline-bounded) fails closed as auth_lost, not a
    # silent proceed onto an unauthenticated page.
    async def _boom(_ctx):
        raise RuntimeError("cdp stalled")

    _nav_env.setattr(cli, "is_logged_in", _boom)
    url = "https://chatgpt.com/c/abc"
    page = _NavPage(final_url=url)
    assert await _recover_navigate(object(), page, url, deadline=await _future_deadline()) == "auth_lost"


async def test_recover_navigate_closed_during_goto(_nav_env):
    url = "https://chatgpt.com/c/abc"
    page = _NavPage(final_url=url, closed=True, goto_exc=RuntimeError("Target closed"))
    assert await _recover_navigate(object(), page, url, deadline=await _future_deadline()) == "closed"


async def test_recover_navigate_nav_timeout_on_live_page(_nav_env):
    url = "https://chatgpt.com/c/abc"
    page = _NavPage(final_url=url, closed=False, goto_exc=PlaywrightTimeoutError("slow"))
    assert await _recover_navigate(object(), page, url, deadline=await _future_deadline()) == "nav_timeout"


async def test_recover_navigate_deadline_exhausted(_nav_env):
    import asyncio
    url = "https://chatgpt.com/c/abc"
    page = _NavPage(final_url=url)
    past = asyncio.get_running_loop().time() - 1.0
    assert await _recover_navigate(object(), page, url, deadline=past) == "deadline"
    assert page.goto_calls == []  # never even navigated once out of budget


# ---- _monitor_and_finalize ----

def _noop_err(reason, extra=None):
    return {"status": "error", "reason": reason, "exit_code": 1, **(extra or {})}


class _FinalizePage:
    def __init__(self, *, url="https://chatgpt.com/c/abc", closed=False):
        self._url = url
        self._closed = closed

    @property
    def url(self):
        return self._url

    def is_closed(self):
        return self._closed

    async def content(self):
        return "<html></html>"


@pytest.fixture
def _finalize_env(monkeypatch):
    async def _noop_shot(_page, _path, **_kw):
        return None

    monkeypatch.setattr(cli, "safe_screenshot", _noop_shot)
    return monkeypatch


def test_monitor_and_finalize_cannot_resubmit():
    # Structural no-resubmit guarantee: the post-send helper receives no prompt,
    # composer, or send handle, so it is *incapable* of pasting or re-sending.
    params = set(inspect.signature(_monitor_and_finalize).parameters)
    for forbidden in ("prompt_text", "prompt", "composer", "send_btn", "ctx"):
        assert forbidden not in params


async def test_monitor_completes_and_audits(_finalize_env, tmp_path):
    import asyncio
    _finalize_env.setattr(cli, "COMPLETION_STABLE_SECS", 0.0)

    async def _text(_page):
        return "the answer"

    async def _stop(_page):
        return 0

    async def _copy_present(_page):
        return True

    async def _copy_extract(_page):
        return "the answer (markdown)"

    async def _slug(_page):
        return "gpt-5-6-pro"

    _finalize_env.setattr(cli, "read_latest_assistant_text", _text)
    _finalize_env.setattr(cli, "_stop_button_count", _stop)
    _finalize_env.setattr(cli, "_copy_button_present", _copy_present)
    _finalize_env.setattr(cli, "_copy_button_extract", _copy_extract)
    _finalize_env.setattr(cli, "served_assistant_model_slug", _slug)

    page = _FinalizePage()
    conv = _ConversationUrl()
    deadline = asyncio.get_running_loop().time() + 100.0
    result = await _monitor_and_finalize(
        page, run_dir=tmp_path, run_id="r1",
        deadline=deadline, send_ts=asyncio.get_running_loop().time(),
        conv=conv, err=_noop_err,
    )
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["extraction"] == "copy_button"
    assert result["model_audit"] == "verified"
    assert (tmp_path / "response.md").read_text() == "the answer (markdown)"


async def test_monitor_stops_after_send_on_signal(_finalize_env, tmp_path):
    # A stop.request seen post-send halts the turn: the monitor clicks Stop and
    # finalizes `stopped_after_send`. Per the discard policy nothing is extracted
    # or published — no response.md.
    import asyncio

    (tmp_path / cli.STOP_REQUEST).write_text("{}")
    clicked = []

    async def _click(_page):
        clicked.append(True)
        return True

    _finalize_env.setattr(cli, "_click_stop_button", _click)

    page = _FinalizePage()  # url is a valid /c/<id> → landing gate passes
    conv = _ConversationUrl()
    now = asyncio.get_running_loop().time()
    result = await _monitor_and_finalize(
        page, run_dir=tmp_path, run_id="r1",
        deadline=now + 100.0, send_ts=now, conv=conv, err=_noop_err,
    )
    assert result["status"] == "stopped"
    assert result["reason"] == "stopped_after_send"
    assert result["exit_code"] == 5
    assert clicked == [True]
    assert not (tmp_path / "response.md").exists()


async def test_monitor_stop_on_drifted_tab_never_clicks(_finalize_env, tmp_path):
    # A stop on a tab that drifted to a DIFFERENT conversation must NOT click Stop
    # (halting an unowned conversation) — it returns conversation_drift instead.
    import asyncio

    (tmp_path / cli.STOP_REQUEST).write_text("{}")
    clicked = []

    async def _click(_page):
        clicked.append(True)
        return True

    _finalize_env.setattr(cli, "_click_stop_button", _click)

    conv = _ConversationUrl()
    conv.capture("https://chatgpt.com/c/OWNED")             # we own OWNED
    page = _FinalizePage(url="https://chatgpt.com/c/OTHER")  # tab drifted to OTHER
    now = asyncio.get_running_loop().time()
    result = await _monitor_and_finalize(
        page, run_dir=tmp_path, run_id="r1",
        deadline=now + 100.0, send_ts=now, conv=conv, err=_noop_err,
    )
    assert result["reason"] == "conversation_drift"
    assert clicked == []  # never acted on the unowned conversation


async def test_monitor_stop_retries_until_positive_click(_finalize_env, tmp_path):
    # A missing/failed Stop click is NOT proof the turn halted (the Pro thinking
    # phase shows no Stop button). The loop must retry, never finalize `stopped`
    # on a non-click — only a POSITIVE click yields `stopped`.
    import asyncio

    (tmp_path / cli.STOP_REQUEST).write_text("{}")
    calls = []

    async def _click(_page):
        calls.append(True)
        return len(calls) >= 2  # False the first time, True the second

    async def _no_text(_page):
        return ""  # never completes on its own

    _finalize_env.setattr(cli, "_click_stop_button", _click)
    _finalize_env.setattr(cli, "read_latest_assistant_text", _no_text)

    page = _FinalizePage()
    conv = _ConversationUrl()
    now = asyncio.get_running_loop().time()
    result = await _monitor_and_finalize(
        page, run_dir=tmp_path, run_id="r1",
        deadline=now + 100.0, send_ts=now, conv=conv, err=_noop_err,
    )
    assert result["status"] == "stopped"  # eventually stopped
    assert len(calls) >= 2                 # did NOT falsely claim stopped on the first (False) click


async def test_monitor_stop_discards_staged_partial(_finalize_env, tmp_path):
    # Discard policy: a stopped run removes any partial staged by a prior attempt
    # so it never leaves a response artifact.
    import asyncio

    (tmp_path / cli.STOP_REQUEST).write_text("{}")
    (tmp_path / cli.RESPONSE_STAGED).write_text("partial from a prior attempt")

    async def _click(_page):
        return True

    _finalize_env.setattr(cli, "_click_stop_button", _click)

    page = _FinalizePage()
    conv = _ConversationUrl()
    now = asyncio.get_running_loop().time()
    result = await _monitor_and_finalize(
        page, run_dir=tmp_path, run_id="r1",
        deadline=now + 100.0, send_ts=now, conv=conv, err=_noop_err,
    )
    assert result["status"] == "stopped"
    assert not (tmp_path / cli.RESPONSE_STAGED).exists()  # staged partial discarded


async def test_monitor_positive_click_survives_close_during_diagnostics(_finalize_env, tmp_path):
    # A CONFIRMED positive Stop click must yield `stopped` even if the tab closes
    # during the post-click diagnostics (screenshot / final.html). The close must
    # NOT raise RunPageClosed into recovery — recovery would lose the confirmed
    # stop and could republish the halted turn as `ok`.
    import asyncio

    (tmp_path / cli.STOP_REQUEST).write_text("{}")

    async def _click(_page):
        return True  # positive confirmation

    _finalize_env.setattr(cli, "_click_stop_button", _click)

    class _CloseOnContentPage(_FinalizePage):
        def is_closed(self):
            return True

        async def content(self):
            raise RuntimeError("target closed")  # close during final.html capture

    page = _CloseOnContentPage()
    conv = _ConversationUrl()
    now = asyncio.get_running_loop().time()
    result = await _monitor_and_finalize(
        page, run_dir=tmp_path, run_id="r1",
        deadline=now + 100.0, send_ts=now, conv=conv, err=_noop_err,
    )
    assert result["status"] == "stopped"          # not RunPageClosed, not ok
    assert result["reason"] == "stopped_after_send"
    assert not (tmp_path / "response.md").exists()


async def test_monitor_positive_click_survives_staged_unlink_failure(_finalize_env, tmp_path):
    # A non-FileNotFoundError failure removing the staged partial after a CONFIRMED
    # click must NOT escape and lose the stop — cleanup is best-effort.
    import asyncio

    (tmp_path / cli.STOP_REQUEST).write_text("{}")
    # A directory at the staged path makes unlink() raise OSError, not FileNotFoundError.
    (tmp_path / cli.RESPONSE_STAGED).mkdir()

    async def _click(_page):
        return True

    _finalize_env.setattr(cli, "_click_stop_button", _click)

    page = _FinalizePage()
    conv = _ConversationUrl()
    now = asyncio.get_running_loop().time()
    result = await _monitor_and_finalize(
        page, run_dir=tmp_path, run_id="r1",
        deadline=now + 100.0, send_ts=now, conv=conv, err=_noop_err,
    )
    assert result["status"] == "stopped"


async def test_monitor_closure_raises_run_page_closed(_finalize_env, tmp_path):
    import asyncio

    async def _text_closed(_page):
        raise RunPageClosed()

    _finalize_env.setattr(cli, "read_latest_assistant_text", _text_closed)
    page = _FinalizePage(closed=True)
    conv = _ConversationUrl()
    deadline = asyncio.get_running_loop().time() + 100.0
    with pytest.raises(RunPageClosed):
        await _monitor_and_finalize(
            page, run_dir=tmp_path, run_id="r1",
            deadline=deadline, send_ts=asyncio.get_running_loop().time(),
            conv=conv, err=_noop_err,
        )


async def test_monitor_past_deadline_times_out_without_fresh_budget(_finalize_env, tmp_path):
    # A deadline already in the past must fall straight through to a timeout
    # result (exit 3) — recovery/monitoring never grants a fresh generation
    # budget. served_model audit still runs on the (empty) turn.
    import asyncio

    async def _slug(_page):
        return "gpt-5-6-pro"

    _finalize_env.setattr(cli, "served_assistant_model_slug", _slug)
    page = _FinalizePage()
    conv = _ConversationUrl()
    past = asyncio.get_running_loop().time() - 1.0
    result = await _monitor_and_finalize(
        page, run_dir=tmp_path, run_id="r1",
        deadline=past, send_ts=past, conv=conv, err=_noop_err,
    )
    assert result["status"] == "timeout"
    assert result["exit_code"] == 3


async def test_monitor_conversation_drift_fails_closed(_finalize_env, tmp_path):
    # If the (reopened) page is on a DIFFERENT conversation than the captured
    # one, finalize must refuse — never extract/return another conversation's
    # answer as ok. Guard runs before extraction/audit.
    import asyncio
    page = _FinalizePage(url="https://chatgpt.com/c/OTHER")
    conv = _ConversationUrl()
    conv.capture("https://chatgpt.com/c/abc")
    past = asyncio.get_running_loop().time() - 1.0
    result = await _monitor_and_finalize(
        page, run_dir=tmp_path, run_id="r1",
        deadline=past, send_ts=past, conv=conv, err=_noop_err,
    )
    assert result["reason"] == "conversation_drift"
    assert result["expected"] == "https://chatgpt.com/c/abc"
    # No answer file written for a drifted conversation.
    assert not (tmp_path / "response.md").exists()


def _fake_turn(env, *, text, copied=None, slug=None, menu=None, copy_present=True):
    """Wire the page-facing helpers `_monitor_and_finalize` calls for one turn.

    `slug`/`menu` may be a value or an exception instance to raise (a page close
    during the audit). `copy_present=False` starves the completion gate, which
    is how a fully-rendered answer still times out when the selector drifts.
    """
    async def _text(_page):
        return text

    async def _stop(_page):
        return 0

    async def _copy_present(_page):
        return copy_present

    async def _copy_extract(_page):
        return copied

    async def _slug(_page):
        if isinstance(slug, BaseException):
            raise slug
        return slug

    async def _menu(_page, timeout=10.0):
        if isinstance(menu, BaseException):
            raise menu
        return menu

    env.setattr(cli, "COMPLETION_STABLE_SECS", 0.0)
    env.setattr(cli, "read_latest_assistant_text", _text)
    env.setattr(cli, "_stop_button_count", _stop)
    env.setattr(cli, "_copy_button_present", _copy_present)
    env.setattr(cli, "_copy_button_extract", _copy_extract)
    env.setattr(cli, "served_assistant_model_slug", _slug)
    env.setattr(cli, "read_selected_model", _menu)


async def _finalize(tmp_path, *, budget=100.0):
    import asyncio
    loop = asyncio.get_running_loop()
    return await _monitor_and_finalize(
        _FinalizePage(), run_dir=tmp_path, run_id="r1",
        deadline=loop.time() + budget, send_ts=loop.time(),
        conv=_ConversationUrl(), err=_noop_err,
    )


def _artifacts(run_dir):
    """Which response artifact(s) a run published. `response.md` means exactly
    one thing — a verified, completion-gated answer — so the whole point is that
    every other outcome lands under a different name."""
    return {p.name for p in run_dir.glob("response*.md")}


async def test_monitor_publishes_canonical_only_when_verified_and_complete(_finalize_env, tmp_path):
    _fake_turn(_finalize_env, text="the answer", copied="the answer (markdown)",
               slug="gpt-5-6-pro")
    result = await _finalize(tmp_path)
    assert result["status"] == "ok"
    assert result["model_audit"] == "verified"
    assert _artifacts(tmp_path) == {"response.md"}
    assert (tmp_path / "response.md").read_text() == "the answer (markdown)"


async def test_monitor_quarantines_answer_on_model_mismatch(_finalize_env, tmp_path):
    # A rejected turn's text is COMPLETE and plausible — it differs from a
    # verified answer only by provenance. Rejecting the run is not enough: the
    # artifact must not reach `response.md`, the name every consumer treats as
    # "the answer" (run ask-20260716T034413Z, 2026-07-15: a caller hit this
    # reason and reached for response.md out of the run dir).
    _fake_turn(_finalize_env, text="wrong-model answer",
               copied="wrong-model answer (markdown)", slug="gpt-5-5-pro")
    result = await _finalize(tmp_path)
    assert result["reason"] == "served_model_mismatch"
    assert result["served_slug"] == "gpt-5-5-pro"
    assert _artifacts(tmp_path) == {"response.rejected.md"}
    assert (tmp_path / "response.rejected.md").read_text() == "wrong-model answer (markdown)"
    # Reporting the path would hand a recovery agent the salvage target in a
    # field named for the answer; run_dir + the fixed name already suffice.
    assert "rejected_response" not in result
    assert result["response_chars"] == len("wrong-model answer (markdown)")


async def test_monitor_quarantines_answer_on_menu_mismatch(_finalize_env, tmp_path):
    # Same quarantine on the slug-absent branch: the menu confirmed a non-Sol
    # model, equally fatal and equally salvageable off disk.
    _fake_turn(_finalize_env, text="wrong-model answer",
               copied="wrong-model answer (markdown)", slug=None, menu="GPT-5.5")
    result = await _finalize(tmp_path)
    assert result["reason"] == "model_menu_mismatch"
    assert _artifacts(tmp_path) == {"response.rejected.md"}


async def test_monitor_publishes_canonical_on_fail_open_audit(_finalize_env, tmp_path):
    # The fail-OPEN verdicts must still publish: a double selector break (slug
    # attribute renamed AND menu unreadable) returns ok by design, so its
    # terminal status authorizes `response.md`. Pins that the lifecycle tracks
    # the FATAL verdicts only and can't brick the tool on a rename.
    _fake_turn(_finalize_env, text="the answer", copied="the answer (markdown)",
               slug=None, menu=None)
    result = await _finalize(tmp_path)
    assert result["status"] == "ok"
    assert result["model_audit"] == "unverified_missing_slug"
    assert _artifacts(tmp_path) == {"response.md"}


async def test_monitor_close_during_audit_publishes_nothing(_finalize_env, tmp_path):
    # The audit reads the page AFTER extraction, so a close there raises before
    # any verdict exists. The text must not reach `response.md`: it is UNAUDITED
    # — neither model nor effort is proven — and if recovery then fails to open a
    # fresh tab the run ends terminally (browser_disconnected_after_send) with
    # that file as its most answer-looking artifact. It stays staged instead, so
    # the diagnostic survives without ever claiming to be the answer.
    _fake_turn(_finalize_env, text="unaudited answer",
               copied="unaudited answer (markdown)", slug=RunPageClosed())
    with pytest.raises(RunPageClosed):
        await _finalize(tmp_path)
    assert "response.md" not in _artifacts(tmp_path)
    assert (tmp_path / "response.pending.md").read_text() == "unaudited answer (markdown)"


async def test_monitor_timeout_publishes_partial_not_canonical(_finalize_env, tmp_path):
    # Completion needs the Copy button, so a drifted selector times out a turn
    # whose text is FULLY rendered — "partial" is not visibly partial (cf. the
    # 228-char thinking-summary fragment of reframe-review-040). The model is
    # right here; the answer is simply not completion-gated, so it must not take
    # the canonical name. Needs a live budget: the monitor loop has to actually
    # run to accumulate text.
    rendered = "A complete-looking answer. " * 20
    _fake_turn(_finalize_env, text=rendered, copy_present=False, slug="gpt-5-6-pro")
    result = await _finalize(tmp_path, budget=0.25)
    assert result["status"] == "timeout"
    assert result["exit_code"] == 3
    assert _artifacts(tmp_path) == {"response.partial.md"}
    assert (tmp_path / "response.partial.md").read_text() == rendered


async def test_postsend_recovery_across_attempts_publishes_one_artifact(_finalize_env, tmp_path):
    # Cross-ATTEMPT artifact uniqueness. The single-attempt tests above call
    # _monitor_and_finalize once, and the _run_postsend tests below swap in
    # _ScriptedMonitor, which never touches the filesystem — so nothing else
    # covers the seam where attempt 1 stages a body, dies in the audit, and
    # attempt 2 re-stages and publishes on a fresh tab. Exactly one artifact may
    # survive: a leftover from attempt 1 alongside attempt 2's answer would put
    # a stale body in the run_dir under a name a caller might read.
    import asyncio
    audits = {"n": 0}

    async def _slug_closes_once(_page):
        audits["n"] += 1
        if audits["n"] == 1:
            raise RunPageClosed()
        return "gpt-5-6-pro"

    async def _nav_ok(_ctx, _page, _url, *, deadline):
        return None

    _fake_turn(_finalize_env, text="the answer", copied="the answer (markdown)")
    _finalize_env.setattr(cli, "served_assistant_model_slug", _slug_closes_once)
    _finalize_env.setattr(cli, "_attach_response_logger", lambda _p, _n: None)
    _finalize_env.setattr(cli, "_recover_navigate", _nav_ok)

    conv = _ConversationUrl()
    conv.capture("https://chatgpt.com/c/abc")
    loop = asyncio.get_running_loop()
    result, _page = await cli._run_postsend(
        _FakeCtx(pages=[_FinalizePage()]), _FinalizePage(),
        run_dir=tmp_path, run_id="r1", deadline=loop.time() + 100.0,
        send_ts=loop.time(), conv=conv, network_log=[], err=_noop_err,
    )
    assert result["status"] == "ok"
    assert audits["n"] == 2, "the audit must actually re-run on the recovered tab"
    assert _artifacts(tmp_path) == {"response.md"}


async def test_monitor_mismatch_outranks_timeout(_finalize_env, tmp_path):
    # A wrong model on an ungated turn is a model failure first: the fatal audit
    # runs regardless of `completed`, so this quarantines rather than publishing
    # a partial.
    _fake_turn(_finalize_env, text="wrong-model partial", copy_present=False,
               slug="gpt-5-5-pro")
    result = await _finalize(tmp_path, budget=0.25)
    assert result["reason"] == "served_model_mismatch"
    assert _artifacts(tmp_path) == {"response.rejected.md"}


# ---- _confirm_send_landed (post-send landing gate) ----

class _LandingPage:
    """Minimal page for the landing gate: exposes `url` + `is_closed`. The two
    DOM signals (_user_turn_present / _stop_button_count) are monkeypatched, so
    no locator fake is needed."""
    def __init__(self, url="https://chatgpt.com/", closed=False):
        self._url = url
        self._closed = closed

    @property
    def url(self):
        return self._url

    def is_closed(self):
        return self._closed


@pytest.fixture
def _landing_env(monkeypatch):
    # Default: no user turn, no Stop button, zero-length window so a no-signal
    # gate falls straight through to the out-of-budget return without a
    # wall-clock wait. Tests that exercise the DOM probes / polling raise the
    # window (a positive SEND_LANDING_TIMEOUT).
    async def _no_user(_page):
        return False

    async def _no_stop(_page):
        return 0

    monkeypatch.setattr(cli, "_user_turn_present", _no_user)
    monkeypatch.setattr(cli, "_stop_button_count", _no_stop)
    monkeypatch.setattr(cli, "SEND_LANDING_TIMEOUT", 0.0)
    return monkeypatch


async def test_send_landed_via_url(_landing_env):
    import asyncio
    page = _LandingPage(url="https://chatgpt.com/c/abc")
    conv = _ConversationUrl()
    loop = asyncio.get_running_loop()
    assert await _confirm_send_landed(page, conv, deadline=loop.time() + 100.0) is True
    assert conv.get() == "https://chatgpt.com/c/abc"


async def test_send_not_landed_when_no_signal(_landing_env):
    # Zero-window (out-of-budget) case: composer full, URL still `/`, no user
    # turn / Stop → not landed. The DOM-checked-then-timed-out path is covered by
    # test_send_landing_sleep_clamped_to_budget below.
    import asyncio
    page = _LandingPage(url="https://chatgpt.com/")
    conv = _ConversationUrl()
    loop = asyncio.get_running_loop()
    assert await _confirm_send_landed(page, conv, deadline=loop.time() + 100.0) is False


async def test_send_landed_via_user_turn(_landing_env):
    # A rendered user turn proves landing even before the URL flips to /c/. Needs
    # a positive window: the cutoff is checked before the DOM probes.
    import asyncio
    _landing_env.setattr(cli, "SEND_LANDING_TIMEOUT", 5.0)

    async def _yes_user(_page):
        return True

    _landing_env.setattr(cli, "_user_turn_present", _yes_user)
    conv = _ConversationUrl()
    loop = asyncio.get_running_loop()
    assert await _confirm_send_landed(_LandingPage(), conv, deadline=loop.time() + 100.0) is True


async def test_send_landed_via_stop_button(_landing_env):
    import asyncio
    _landing_env.setattr(cli, "SEND_LANDING_TIMEOUT", 5.0)

    async def _stop(_page):
        return 1

    _landing_env.setattr(cli, "_stop_button_count", _stop)
    conv = _ConversationUrl()
    loop = asyncio.get_running_loop()
    assert await _confirm_send_landed(_LandingPage(), conv, deadline=loop.time() + 100.0) is True


async def test_send_landed_via_delayed_signal(_landing_env):
    # The polling path: the signal is absent on the first probe and appears on a
    # later one. Proves the gate keeps polling within budget rather than
    # one-shotting. (asyncio.sleep is the autouse no-op, so no wall-clock wait.)
    import asyncio
    _landing_env.setattr(cli, "SEND_LANDING_TIMEOUT", 100.0)
    calls = {"n": 0}

    async def _user_delayed(_page):
        calls["n"] += 1
        return calls["n"] >= 2

    _landing_env.setattr(cli, "_user_turn_present", _user_delayed)
    conv = _ConversationUrl()
    loop = asyncio.get_running_loop()
    assert await _confirm_send_landed(_LandingPage(), conv, deadline=loop.time() + 100.0) is True
    assert calls["n"] >= 2


async def test_send_not_landed_past_deadline_skips_dom(_landing_env):
    # An already-past deadline must return False WITHOUT touching the page — the
    # cutoff is checked before any DOM read so a spent budget never overruns.
    import asyncio

    async def _boom(_page):
        raise AssertionError("no DOM read when the budget is already spent")

    _landing_env.setattr(cli, "_user_turn_present", _boom)
    _landing_env.setattr(cli, "_stop_button_count", _boom)
    _landing_env.setattr(cli, "SEND_LANDING_TIMEOUT", 100.0)
    conv = _ConversationUrl()  # empty: no URL captured
    past = asyncio.get_running_loop().time() - 1.0
    assert await _confirm_send_landed(_LandingPage(url="https://chatgpt.com/"),
                                      conv, deadline=past) is False


async def test_send_landing_sleep_clamped_to_budget(_landing_env):
    # A remaining budget shorter than `poll` must clamp the sleep so the gate
    # returns at the cutoff, never a full `poll` past it. Pre-fix this slept the
    # whole 0.5s poll regardless of the 0.05s budget. Records every sleep
    # argument and asserts none exceeds the window.
    import asyncio
    _landing_env.setattr(cli, "SEND_LANDING_TIMEOUT", 0.05)
    slept = []

    async def _rec(d):  # overrides the autouse no-op; records the clamp arg
        slept.append(d)
        return None

    _landing_env.setattr(cli.asyncio, "sleep", _rec)
    probes = {"n": 0}

    async def _count(_page):
        probes["n"] += 1
        return False

    _landing_env.setattr(cli, "_user_turn_present", _count)
    conv = _ConversationUrl()
    loop = asyncio.get_running_loop()
    result = await _confirm_send_landed(_LandingPage(), conv,
                                        deadline=loop.time() + 100.0, poll=0.5)
    assert result is False
    assert probes["n"] >= 1            # the DOM was actually probed before timeout
    assert slept and max(slept) <= 0.05  # never slept a full 0.5 poll past cutoff


async def test_send_landing_noop_on_preset_conv(_landing_env):
    # Recovery re-entry: conv already captured → instant True, and the DOM
    # signals are never even read (so a closed page can't raise here).
    import asyncio
    _landing_env.setattr(cli, "SEND_LANDING_TIMEOUT", 100.0)

    async def _boom(_page):
        raise AssertionError("must not read DOM once conv is set")

    _landing_env.setattr(cli, "_user_turn_present", _boom)
    _landing_env.setattr(cli, "_stop_button_count", _boom)
    conv = _ConversationUrl()
    conv.capture("https://chatgpt.com/c/abc")
    loop = asyncio.get_running_loop()
    page = _LandingPage(url="https://chatgpt.com/", closed=True)
    assert await _confirm_send_landed(page, conv, deadline=loop.time() + 100.0) is True


async def test_send_landing_close_raises_run_page_closed(_landing_env):
    # A tab close during the gate (before any URL was captured) must raise so the
    # recovery loop handles it — never silently report not-landed.
    import asyncio
    _landing_env.setattr(cli, "SEND_LANDING_TIMEOUT", 100.0)

    async def _closed(_page):
        raise RunPageClosed()

    _landing_env.setattr(cli, "_user_turn_present", _closed)
    conv = _ConversationUrl()
    loop = asyncio.get_running_loop()
    with pytest.raises(RunPageClosed):
        await _confirm_send_landed(_LandingPage(), conv, deadline=loop.time() + 100.0)


class _LocatorPage:
    """Page whose `.locator(...).count()` is scriptable — drives the real
    `_user_turn_present` (rather than a monkeypatched stand-in) through its
    sentinel/close branches."""
    def __init__(self, *, count=0, raises=False, closed=False):
        self._count = count
        self._raises = raises
        self._closed = closed

    def is_closed(self):
        return self._closed

    def locator(self, _selector):
        page = self

        class _Loc:
            async def count(self):
                if page._raises:
                    raise RuntimeError("transient locator failure")
                return page._count

        return _Loc()


async def test_user_turn_present_reports_count():
    assert await cli._user_turn_present(_LocatorPage(count=1)) is True
    assert await cli._user_turn_present(_LocatorPage(count=0)) is False


async def test_user_turn_present_live_transient_returns_false():
    # A read failure on a still-live page is a sentinel False (keeps the gate
    # polling), never a raise.
    assert await cli._user_turn_present(_LocatorPage(raises=True, closed=False)) is False


async def test_user_turn_present_closed_raises_run_page_closed():
    with pytest.raises(RunPageClosed):
        await cli._user_turn_present(_LocatorPage(raises=True, closed=True))


async def test_monitor_fails_closed_on_send_not_landed(_finalize_env, tmp_path):
    # End-to-end through the monitor: a no-op Send fails closed FAST as
    # send_did_not_land instead of spinning to the generation deadline, and
    # publishes NO answer artifact (it returns before any staging).
    import asyncio

    async def _no_user(_page):
        return False

    async def _no_stop(_page):
        return 0

    _finalize_env.setattr(cli, "_user_turn_present", _no_user)
    _finalize_env.setattr(cli, "_stop_button_count", _no_stop)
    _finalize_env.setattr(cli, "SEND_LANDING_TIMEOUT", 0.0)
    page = _FinalizePage(url="https://chatgpt.com/")  # never left the landing page
    conv = _ConversationUrl()
    loop = asyncio.get_running_loop()
    result = await _monitor_and_finalize(
        page, run_dir=tmp_path, run_id="r1",
        deadline=loop.time() + 100.0, send_ts=loop.time(),
        conv=conv, err=_noop_err,
    )
    assert result["reason"] == "send_did_not_land"
    assert _artifacts(tmp_path) == set()


# ---- _run_postsend recovery-loop control flow ----

class _ScriptedMonitor:
    """Async stand-in for _monitor_and_finalize. Each call consumes the next
    scripted action: "close" → raise RunPageClosed; a dict → return it. Records
    the page it was handed on each call so tests can assert rebind + that the
    dead-during-nav tab is never re-monitored."""

    def __init__(self, actions):
        self._actions = list(actions)
        self.calls = []

    async def __call__(self, page, **_kwargs):
        self.calls.append(page)
        action = self._actions.pop(0)
        if action == "close":
            raise RunPageClosed()
        return action


class _ScriptedRecover:
    """Async stand-in for _recover_navigate returning scripted reason strings."""

    def __init__(self, reasons):
        self._reasons = list(reasons)
        self.calls = 0

    async def __call__(self, ctx, page, conv_url, *, deadline):
        self.calls += 1
        return self._reasons.pop(0)


class _FakeCtx:
    def __init__(self, pages=None, raise_on_new=False):
        self._pages = list(pages or [])
        self._raise = raise_on_new
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        if self._raise:
            raise RuntimeError("context disconnected")
        return self._pages.pop(0) if self._pages else object()


@pytest.fixture
def _postsend_env(monkeypatch):
    monkeypatch.setattr(cli, "_attach_response_logger", lambda _p, _n: None)

    # Record every page the modal dismissers are installed on so recovery tests
    # can assert each reopened tab is re-protected (like the response logger),
    # not just the initial one. Stubbing the single entry point is deliberate: it
    # is what the recovery path calls, so a future dismisser added there is
    # covered by these assertions for free.
    installs = []

    async def _record_install(page):
        installs.append(page)

    monkeypatch.setattr(cli, "_install_modal_dismissers", _record_install)
    monkeypatch.modal_dismisser_installs = installs
    return monkeypatch


async def _run_postsend(env, *, actions, reasons=(), ctx, conv_url, tmp_path, p0=None):
    import asyncio
    monitor = _ScriptedMonitor(actions)
    recover = _ScriptedRecover(reasons)
    env.setattr(cli, "_monitor_and_finalize", monitor)
    env.setattr(cli, "_recover_navigate", recover)
    conv = _ConversationUrl()
    if conv_url:
        conv.capture(conv_url)
    p0 = p0 or object()
    now = asyncio.get_running_loop().time()
    deadline = now + 100.0 if conv_url != "PAST" else now - 1.0
    if conv_url == "PAST":
        conv.capture("https://chatgpt.com/c/abc")
    result, page = await cli._run_postsend(
        ctx, p0, run_dir=tmp_path, run_id="r1", deadline=deadline,
        send_ts=now, conv=conv, network_log=[], err=_noop_err,
    )
    return result, page, monitor, recover, p0


async def test_postsend_happy_path_no_recovery(_postsend_env, tmp_path):
    ctx = _FakeCtx()
    result, page, monitor, _rec, p0 = await _run_postsend(
        _postsend_env, actions=[{"status": "ok", "exit_code": 0}],
        ctx=ctx, conv_url="https://chatgpt.com/c/abc", tmp_path=tmp_path,
    )
    assert result["status"] == "ok"
    assert page is p0                 # no rebind
    assert ctx.new_page_calls == 0    # no recovery
    assert monitor.calls == [p0]


async def test_postsend_recovers_once_returns_new_page(_postsend_env, tmp_path):
    p1 = object()
    ctx = _FakeCtx(pages=[p1])
    result, page, monitor, recover, p0 = await _run_postsend(
        _postsend_env, actions=["close", {"status": "ok", "exit_code": 0}],
        reasons=[None], ctx=ctx, conv_url="https://chatgpt.com/c/abc", tmp_path=tmp_path,
    )
    assert result["status"] == "ok"
    assert ctx.new_page_calls == 1
    assert page is p1                  # returns the LATEST owned page for cleanup
    assert monitor.calls == [p0, p1]   # re-finalized on the reopened tab
    assert p1 in _postsend_env.modal_dismisser_installs  # re-installed on the reopened tab


async def test_postsend_no_url_terminal_no_reopen(_postsend_env, tmp_path):
    ctx = _FakeCtx()
    result, page, monitor, _rec, p0 = await _run_postsend(
        _postsend_env, actions=["close"], ctx=ctx, conv_url=None, tmp_path=tmp_path,
    )
    assert result["reason"] == "page_closed_before_conversation_url"
    assert ctx.new_page_calls == 0     # never guesses / never reopens without a URL
    assert page is p0


async def test_postsend_browser_disconnected(_postsend_env, tmp_path):
    ctx = _FakeCtx(raise_on_new=True)
    result, _page, _monitor, _rec, _p0 = await _run_postsend(
        _postsend_env, actions=["close"], ctx=ctx,
        conv_url="https://chatgpt.com/c/abc", tmp_path=tmp_path,
    )
    assert result["reason"] == "browser_disconnected_after_send"
    assert ctx.new_page_calls == 1


async def test_postsend_exhausts_budget(_postsend_env, tmp_path):
    # Monitor closes every time, reopen always succeeds → exhaust MAX_PAGE_RECOVERIES.
    pages = [object() for _ in range(cli.MAX_PAGE_RECOVERIES)]
    ctx = _FakeCtx(pages=pages)
    result, _page, monitor, recover, _p0 = await _run_postsend(
        _postsend_env,
        actions=["close"] * (cli.MAX_PAGE_RECOVERIES + 1),
        reasons=[None] * cli.MAX_PAGE_RECOVERIES,
        ctx=ctx, conv_url="https://chatgpt.com/c/abc", tmp_path=tmp_path,
    )
    assert result["reason"] == "page_recovery_exhausted"
    assert result["attempts"] == cli.MAX_PAGE_RECOVERIES
    assert ctx.new_page_calls == cli.MAX_PAGE_RECOVERIES
    assert len(monitor.calls) == cli.MAX_PAGE_RECOVERIES + 1


async def test_postsend_closed_during_nav_consumes_one_slot(_postsend_env, tmp_path):
    # The closed_during_nav fix: a reopen that closes during navigation must
    # re-classify WITHOUT re-entering the monitor on that dead tab.
    p1, p2 = object(), object()
    ctx = _FakeCtx(pages=[p1, p2])
    result, page, monitor, recover, p0 = await _run_postsend(
        _postsend_env, actions=["close", {"status": "ok", "exit_code": 0}],
        reasons=["closed", None], ctx=ctx,
        conv_url="https://chatgpt.com/c/abc", tmp_path=tmp_path,
    )
    assert result["status"] == "ok"
    assert ctx.new_page_calls == 2           # p1 (closed during nav), then p2 (good)
    assert recover.calls == 2
    assert monitor.calls == [p0, p2]         # p1 (dead) is NEVER re-monitored
    assert page is p2


async def test_postsend_recovery_failed_on_redirect(_postsend_env, tmp_path):
    p1 = object()
    ctx = _FakeCtx(pages=[p1])
    result, page, _monitor, _rec, _p0 = await _run_postsend(
        _postsend_env, actions=["close"], reasons=["redirect"], ctx=ctx,
        conv_url="https://chatgpt.com/c/abc", tmp_path=tmp_path,
    )
    assert result["reason"] == "page_recovery_failed"
    assert result["recovery_reason"] == "redirect"
    assert page is p1


async def test_postsend_deadline_during_recovery_times_out(_postsend_env, tmp_path):
    # Deadline already spent when the close is detected → timeout, no reopen,
    # never a fresh budget.
    ctx = _FakeCtx()
    result, _page, _monitor, _rec, _p0 = await _run_postsend(
        _postsend_env, actions=["close"], ctx=ctx, conv_url="PAST", tmp_path=tmp_path,
    )
    assert result["status"] == "timeout"
    assert result["exit_code"] == 3
    assert result["reason"] == "deadline_during_recovery"
    assert ctx.new_page_calls == 0
