"""Regression tests for the model-selection chip read.

The contaminated run ask-20260531T065451Z read the composer chip as
"Extended Pro" ~400ms after login, then sent the prompt 2.6s later to a chip
that had re-resolved to "Thinking" (served gpt-5-5-thinking). The old
read_composer_chip_text returned the *first* non-placeholder value it saw,
catching the optimistic-hydration transient. These tests pin the stability
behavior that fixes it: the chip text must repeat for `stable_polls` reads
before it is trusted.

Since the 2026-09 GPT-6 rollout the chip shows the model generation plus the
reasoning-EFFORT tier (for example, "6\nPro"); the selected model radio is
"Latest", and the served slug is gpt-6-pro. The predicate
`is_pro_label` accepts any chip containing the "Pro" (top-tier) token; the same
optimistic-hydrate → re-resolve race can now drift the effort from "Pro" down to
a lower tier, so the stability requirement still applies.
"""

import pytest

from gpt_pro import cli
from gpt_pro.cli import (
    classify_model_status,
    classify_served_audit,
    doctor_exit_ok,
    is_pro_label,
    read_composer_chip_text,
)


class _FakeChip:
    """Returns a scripted sequence of chip texts, cycling once exhausted.

    Cycling lets a short script model an indefinitely oscillating chip (for the
    timeout fail-closed test) while a script that ends on a repeated value still
    reaches a stable streak before it wraps.
    """

    def __init__(self, texts):
        self._texts = list(texts)
        self._i = 0

    async def wait_for(self, **_kwargs):
        return None

    async def inner_text(self):
        text = self._texts[self._i]
        self._i = (self._i + 1) % len(self._texts)
        return text


class _FakeLocator:
    def __init__(self, chip):
        self.first = chip


class _FakePage:
    def __init__(self, texts):
        self._chip = _FakeChip(texts)

    def locator(self, _selector):
        return _FakeLocator(self._chip)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make the 0.2s poll sleep instant so the tests run fast."""

    async def _instant(_delay):
        return None

    monkeypatch.setattr(cli.asyncio, "sleep", _instant)


async def test_optimistic_transient_is_rejected():
    # The exact failure shape: hydrates to "Pro", then settles to a lower effort
    # tier ("High"). The stable read must return the SETTLED value, not the
    # transient — so the fast path fails and the slow path corrects it.
    page = _FakePage(["Model", "Pro", "High", "High", "High"])
    text = await read_composer_chip_text(page, timeout=30.0)
    assert text == "High"
    assert not is_pro_label(text)


async def test_steady_pro_passes():
    page = _FakePage(["Model", "Pro", "Pro", "Pro"])
    text = await read_composer_chip_text(page, timeout=30.0)
    assert text == "Pro"
    assert is_pro_label(text)


async def test_stable_polls_one_is_eager():
    # The slow-path post-click confirmation wants the old eager behavior:
    # return the first non-placeholder value (no hydration race after a
    # deliberate menu click).
    page = _FakePage(["Model", "Pro", "Pro"])
    text = await read_composer_chip_text(page, timeout=30.0, stable_polls=1)
    assert text == "Pro"


async def test_never_hydrates_returns_empty():
    # Chip stuck on the SSR placeholder -> times out, returns "" so the
    # caller's predicate fails closed.
    page = _FakePage(["Model"])
    text = await read_composer_chip_text(page, timeout=0.05)
    assert text == ""
    assert not is_pro_label(text)


async def test_oscillating_chip_times_out_empty():
    # A chip that never holds one value for `stable_polls` consecutive reads
    # must NOT be accepted on the last lucky sample (the original timeout
    # fail-open). It oscillates Pro <-> High forever, so the read times out and
    # returns "" -> fails closed, even though a transient "Pro" was seen on
    # every other poll.
    page = _FakePage(["Pro", "High"])
    text = await read_composer_chip_text(page, timeout=0.05, stable_polls=3)
    assert text == ""
    assert not is_pro_label(text)


def test_pro_label_passes_predicate():
    # The 2026-07 redesign renders the selected top-tier effort as the bare
    # label "Pro" (contains the "Pro" token).
    assert is_pro_label("Pro")
    assert is_pro_label("6\nPro")


def test_lower_effort_tiers_fail_predicate():
    # Every non-Pro effort tier lacks the "Pro" token and must fail closed.
    for label in ("Instant", "Medium", "High", "Extra High", "Model", ""):
        assert not is_pro_label(label)
    assert not is_pro_label(None)


async def test_steady_pro_passes_with_explicit_stable_polls():
    page = _FakePage(["Model", "Pro", "Pro", "Pro"])
    text = await read_composer_chip_text(page, timeout=1.0, stable_polls=3)
    assert text == "Pro"
    assert is_pro_label(text)


def test_classify_model_status():
    # GPT-6 is exposed through the selected "Latest" model row.
    assert classify_model_status("Latest") == "ok"
    # A confirmed non-Latest model must be flagged so doctor goes red (the status
    # is later matched with .startswith("unexpected")).
    for wrong in ("GPT-5.6 Sol", "GPT-5.5", "GPT-5.4", "o3", "GPT-5.3"):
        assert classify_model_status(wrong).startswith("unexpected")
    # An unreadable menu degrades to a non-fatal "unknown" (not "unexpected"),
    # so a flaky Radix read never fails doctor on its own.
    assert classify_model_status(None) == "unknown"
    assert not classify_model_status(None).startswith("unexpected")


def test_doctor_exit_ok_requires_positive_confirmation():
    # Green ONLY when login + Pro effort chip + Latest model are all confirmed.
    assert doctor_exit_ok(True, "ok", "ok")
    # A confirmed wrong effort or wrong model is red.
    assert not doctor_exit_ok(True, "unexpected: 'High'", "ok")
    assert not doctor_exit_ok(True, "ok", "unexpected: 'GPT-5.5'")
    # An UNconfirmable chip/model (read failed or menu unreadable) is red too —
    # doctor must not report green when it couldn't actually verify the setup.
    assert not doctor_exit_ok(True, "ok", "unknown")
    assert not doctor_exit_ok(True, "ok", "failed: TimeoutError: x")
    assert not doctor_exit_ok(True, "failed: TimeoutError: x", "ok")
    # Not logged in is red regardless of the skipped checks.
    assert not doctor_exit_ok(False, "skipped", "skipped")


def test_classify_served_audit():
    # Present slug is authoritative (encodes model + effort).
    assert classify_served_audit("gpt-6-pro", None) == "verified"
    # A present non-allowlisted slug is fatal, including the previous Pro model.
    assert classify_served_audit("gpt-5-6-thinking", None) == "slug_mismatch"
    assert classify_served_audit("gpt-5-6-pro", None) == "slug_mismatch"
    assert classify_served_audit("gpt-5-5-pro", None) == "slug_mismatch"
    # Missing slug → fall back to the read-only menu model read.
    assert classify_served_audit(None, "Latest") == "model_ok_slug_missing"
    assert classify_served_audit(None, "GPT-5.6 Sol") == "menu_mismatch"
    assert classify_served_audit(None, "GPT-5.5") == "menu_mismatch"  # fatal
    # Missing slug AND unreadable menu → documented fail-open (never bricks).
    assert classify_served_audit(None, None) == "unverified_missing_slug"
    # An EMPTY-string slug is treated as missing (served_assistant_model_slug can
    # return ""), so the menu fallback still guards it.
    assert classify_served_audit("", None) == "unverified_missing_slug"
    assert classify_served_audit("", "GPT-5.5") == "menu_mismatch"
    # Only "slug_mismatch"/"menu_mismatch" are the fatal verdicts.
    fatal = {"slug_mismatch", "menu_mismatch"}
    assert classify_served_audit("gpt-6-pro", None) not in fatal
    assert classify_served_audit(None, "Latest") not in fatal
    assert classify_served_audit(None, None) not in fatal
