"""Regression tests for the ParallelSlot self-count deadlock.

Runs ask-20260601T2010* (and the 200833 batch) all failed ~1s after
`slot_acquired` with "Chrome CDP unresponsive but other workers hold
ParallelSlots; refusing to kill shared Chrome." The shared Chrome's CDP had
genuinely wedged, but recovery could never fire: `ensure_shared_chrome_running`
runs *inside* the worker's own ParallelSlot, and the old `_slots_held()` counted
that own slot as a held one — so every worker (even a lone serial run) saw a slot
held and refused to kill+relaunch. These tests pin the fix: a slot-holding caller
passes `skip_slot_id` so only *other* workers block the kill.
"""

import fcntl

import pytest

from gpt_pro import cli


def _hold(path):
    """Open `path` and take a non-blocking exclusive flock, returning the fd."""
    fd = open(path, "w")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _release(*fds):
    for fd in fds:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


def test_slots_held_skips_own_slot(tmp_path, monkeypatch):
    slots = tmp_path / "slots"
    slots.mkdir()
    monkeypatch.setattr(cli, "SLOT_LOCK_DIR", slots)

    # A lone worker holding only its own slot-0 — the exact failing scenario.
    fd0 = _hold(slots / "slot-0.lock")
    try:
        # Without the skip, the worker counts its own slot: the deadlock bug.
        assert cli._slots_held() is True
        # With the skip, no *other* worker is active, so recovery may proceed.
        assert cli._slots_held(skip_slot_id=0) is False
    finally:
        _release(fd0)


def test_slots_held_still_sees_other_worker(tmp_path, monkeypatch):
    slots = tmp_path / "slots"
    slots.mkdir()
    monkeypatch.setattr(cli, "SLOT_LOCK_DIR", slots)

    # Worker on slot 0 while a *real* second worker holds slot 1.
    fd0 = _hold(slots / "slot-0.lock")
    fd1 = _hold(slots / "slot-1.lock")
    try:
        # The skip must not blind us to the genuinely-active sibling tab.
        assert cli._slots_held(skip_slot_id=0) is True
    finally:
        _release(fd0, fd1)


def test_slots_held_empty_when_nothing_held(tmp_path, monkeypatch):
    slots = tmp_path / "slots"
    slots.mkdir()
    monkeypatch.setattr(cli, "SLOT_LOCK_DIR", slots)

    # Stale, unlocked lock files (left behind after workers exit) are not "held".
    (slots / "slot-0.lock").write_text("")
    (slots / "slot-1.lock").write_text("")
    assert cli._slots_held() is False
    assert cli._slots_held(skip_slot_id=0) is False


def test_slots_held_skips_only_the_named_slot(tmp_path, monkeypatch):
    slots = tmp_path / "slots"
    slots.mkdir()
    monkeypatch.setattr(cli, "SLOT_LOCK_DIR", slots)

    fd0 = _hold(slots / "slot-0.lock")
    fd1 = _hold(slots / "slot-1.lock")
    try:
        # Skipping a non-first slot must not short-circuit the scan of the rest
        # (glob order is filesystem-dependent) — the held sibling still blocks.
        assert cli._slots_held(skip_slot_id=1) is True
        assert cli._slots_held(skip_slot_id=0) is True
    finally:
        _release(fd0, fd1)


class _DummyLock:
    """Stand-in for LaunchLock so the recovery path needs no real launch.lock."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _wire_recovery(monkeypatch, tmp_path):
    """Drive ensure_shared_chrome_running down the wedged-CDP recovery path with
    all real side effects (kill, relaunch) mocked. probe_cdp reports wedged for
    the fast probe + 2 re-probes, then healthy once the relaunch is issued."""
    slots = tmp_path / "slots"
    slots.mkdir(exist_ok=True)
    monkeypatch.setattr(cli, "SLOT_LOCK_DIR", slots)
    # Recovery also requires proving no `login`/`doctor` holds the activity
    # lease, so the recovery path needs a real lock file to convert.
    monkeypatch.setattr(cli, "CHROME_ACTIVITY_LOCK", tmp_path / "chrome-activity.lock")
    beta = tmp_path / "Google Chrome Beta.app"
    monkeypatch.setattr(cli, "chrome_app_path", lambda: beta)
    monkeypatch.setattr(cli, "validate_chrome_app", lambda app: app)
    monkeypatch.setattr(cli, "_require_running_chrome_app", lambda *_a: None)
    monkeypatch.setattr(cli, "LaunchLock", _DummyLock)
    monkeypatch.setattr(cli.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(cli, "log_stage", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "bind_chrome_compositor_surface", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_chrome_open_argv", lambda port: [])
    calls = {"kill": 0, "popen": 0}
    monkeypatch.setattr(cli, "_kill_chrome_orphans",
                        lambda: calls.__setitem__("kill", calls["kill"] + 1))
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *_a, **_k: calls.__setitem__("popen", calls["popen"] + 1))
    seq = {"n": 0}

    def fake_probe(port, timeout=1.0):
        seq["n"] += 1
        return seq["n"] > 3  # wedged for fast-path + 2 retries, then healthy

    monkeypatch.setattr(cli, "probe_cdp", fake_probe)
    return slots, calls


def test_ensure_chrome_recovers_when_only_own_slot_held(tmp_path, monkeypatch):
    slots, calls = _wire_recovery(monkeypatch, tmp_path)
    fd0 = _hold(slots / "slot-0.lock")  # this worker's own slot
    try:
        # Wedged CDP, only our own slot held, and only our own activity lease →
        # must NOT raise; must relaunch. Neither guard may count *itself*.
        with cli.ChromeActivityLease():
            result = cli.ensure_shared_chrome_running(skip_slot_id=0)
    finally:
        _release(fd0)
    assert result is True            # performed the launch (owner return)
    assert calls["kill"] == 1
    assert calls["popen"] == 1


def test_ensure_chrome_refuses_when_sibling_slot_held(tmp_path, monkeypatch):
    slots, calls = _wire_recovery(monkeypatch, tmp_path)
    fd0 = _hold(slots / "slot-0.lock")  # own
    fd1 = _hold(slots / "slot-1.lock")  # a genuinely-active sibling
    try:
        with cli.ChromeActivityLease():
            with pytest.raises(RuntimeError, match="other workers hold ParallelSlots"):
                cli.ensure_shared_chrome_running(skip_slot_id=0)
    finally:
        _release(fd0, fd1)
    assert calls["kill"] == 0         # never killed Chrome out from under the sibling
    assert calls["popen"] == 0
