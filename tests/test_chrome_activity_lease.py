"""Chrome lifetime ownership across workers, diagnostics, and shutdown."""

import multiprocessing
from pathlib import Path

import pytest

from gpt_pro import cli


def _hold_shared_lease(lock_path, ready, release):
    """Child-process holder: flock behavior must be tested across processes."""
    cli.CHROME_ACTIVITY_LOCK = Path(lock_path)
    with cli.ChromeActivityLease():
        ready.set()
        release.wait(10)


def _try_shared_lease(lock_path, result):
    cli.CHROME_ACTIVITY_LOCK = Path(lock_path)
    lease = cli.ChromeActivityLease(blocking=False)
    acquired = lease.acquire()
    result.put(acquired)
    if acquired:
        lease.release()


class _DummyLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_close_chrome_refuses_while_shared_activity_is_held(tmp_path, monkeypatch):
    lock_path = tmp_path / "chrome-activity.lock"
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    child = ctx.Process(target=_hold_shared_lease, args=(lock_path, ready, release))
    child.start()
    assert ready.wait(5), "child never acquired the shared Chrome lease"

    calls = {"kill": 0}
    monkeypatch.setattr(cli, "CHROME_ACTIVITY_LOCK", lock_path)
    monkeypatch.setattr(cli, "LaunchLock", _DummyLock)
    monkeypatch.setattr(cli, "_slots_held", lambda: False)
    monkeypatch.setattr(
        cli,
        "_kill_chrome_orphans",
        lambda: calls.__setitem__("kill", calls["kill"] + 1),
    )
    try:
        assert cli.cmd_close_chrome() == 1
        assert calls["kill"] == 0
        assert cli.cmd_close_chrome(force=True) == 0
        assert calls["kill"] == 1
    finally:
        release.set()
        child.join(5)
        if child.is_alive():
            child.terminate()
            child.join(5)
    assert child.exitcode == 0

    assert cli.cmd_close_chrome() == 0
    assert calls["kill"] == 2


def test_exclusive_shutdown_lease_blocks_new_browser_user(tmp_path, monkeypatch):
    lock_path = tmp_path / "chrome-activity.lock"
    monkeypatch.setattr(cli, "CHROME_ACTIVITY_LOCK", lock_path)
    with cli.ChromeActivityLease(exclusive=True):
        ctx = multiprocessing.get_context("spawn")
        result = ctx.Queue()
        child = ctx.Process(target=_try_shared_lease, args=(lock_path, result))
        child.start()
        assert result.get(timeout=5) is False
        child.join(5)
        if child.is_alive():
            child.terminate()
            child.join(5)
        assert child.exitcode == 0


def _wire_recovery(monkeypatch, tmp_path, lock_path):
    """Drive ensure_shared_chrome_running down the wedged-CDP recovery path with
    every real side effect mocked. No slots exist, so ParallelSlot cannot be what
    stops the kill — only the activity lease can."""
    app = tmp_path / "Google Chrome Beta.app"
    slots = tmp_path / "slots"
    slots.mkdir(exist_ok=True)
    monkeypatch.setattr(cli, "SLOT_LOCK_DIR", slots)
    monkeypatch.setattr(cli, "CHROME_ACTIVITY_LOCK", lock_path)
    monkeypatch.setattr(cli, "chrome_app_path", lambda: app)
    monkeypatch.setattr(cli, "validate_chrome_app", lambda a: a)
    monkeypatch.setattr(cli, "_require_running_chrome_app", lambda *_a: None)
    monkeypatch.setattr(cli, "LaunchLock", _DummyLock)
    monkeypatch.setattr(cli, "_chrome_open_argv", lambda port: [])
    monkeypatch.setattr(cli, "bind_chrome_compositor_surface", lambda *_a, **_k: None)
    monkeypatch.setattr(cli.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(cli, "log_stage", lambda *_a, **_k: None)
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
    return calls


def test_recovery_refuses_to_kill_under_a_lease_only_user(tmp_path, monkeypatch):
    """The bug: `login`/`doctor` hold an activity lease and take NO ParallelSlot,
    so a slot-only guard let a worker kill Chrome out from under a human halfway
    through signing in. Reverting the lease check makes this test kill and fail."""
    lock_path = tmp_path / "chrome-activity.lock"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold_shared_lease, args=(lock_path, ready, release))
    child.start()
    assert ready.wait(5), "child never acquired the shared Chrome lease"

    calls = _wire_recovery(monkeypatch, tmp_path, lock_path)
    try:
        with cli.ChromeActivityLease():  # the worker's own lease, as in production
            with pytest.raises(RuntimeError, match="is using the shared browser"):
                cli.ensure_shared_chrome_running()
    finally:
        release.set()
        child.join(5)
        if child.is_alive():
            child.terminate()
            child.join(5)
    assert calls["kill"] == 0, "killed Chrome while login/doctor was using it"
    assert calls["popen"] == 0


def test_recovery_proceeds_when_sole_activity_user(tmp_path, monkeypatch):
    """The other half: holding only our OWN lease must not read as contention.

    A second fd in this process would conflict with our own LOCK_SH and block
    recovery forever — the `_slots_held` self-count trap in a new costume.
    """
    lock_path = tmp_path / "chrome-activity.lock"
    calls = _wire_recovery(monkeypatch, tmp_path, lock_path)
    with cli.ChromeActivityLease() as lease:
        assert cli.ensure_shared_chrome_running() is True
        # Recovery must hand the lease back as shared, or the next `close-chrome`
        # would see a still-exclusive lock and the worker could never be joined.
        assert lease.exclusive is False
    assert calls["kill"] == 1
    assert calls["popen"] == 1


def test_recovery_refuses_when_process_holds_no_lease(tmp_path, monkeypatch):
    """No lease at all means we cannot prove sole use — fail closed, don't kill."""
    calls = _wire_recovery(monkeypatch, tmp_path, tmp_path / "chrome-activity.lock")
    with pytest.raises(RuntimeError, match="no activity lease"):
        cli.ensure_shared_chrome_running()
    assert calls["kill"] == 0


def test_upgraded_lease_blocks_a_new_browser_user_during_the_kill(tmp_path, monkeypatch):
    """The upgrade is not just a probe — holding it exclusively across the kill
    is what keeps a new `login` out of the check-to-kill window."""
    lock_path = tmp_path / "chrome-activity.lock"
    monkeypatch.setattr(cli, "CHROME_ACTIVITY_LOCK", lock_path)
    ctx = multiprocessing.get_context("spawn")
    with cli.ChromeActivityLease() as lease:
        assert lease.try_upgrade_exclusive() is True
        result = ctx.Queue()
        child = ctx.Process(target=_try_shared_lease, args=(lock_path, result))
        child.start()
        assert result.get(timeout=5) is False, "a new user entered during the kill window"
        child.join(5)
        if child.is_alive():
            child.terminate()
            child.join(5)

        lease.downgrade_shared()
        result2 = ctx.Queue()
        child2 = ctx.Process(target=_try_shared_lease, args=(lock_path, result2))
        child2.start()
        assert result2.get(timeout=5) is True, "downgrade did not readmit other users"
        child2.join(5)
        if child2.is_alive():
            child2.terminate()
            child2.join(5)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [cli.cmd_login, cli.cmd_doctor])
async def test_login_and_doctor_acquire_activity_before_ensure(command, tmp_path, monkeypatch):
    events = []

    class _RecordingLease:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *_):
            events.append("exit")

    def fail_ensure():
        assert events == ["enter"]
        raise RuntimeError("stop after lease assertion")

    monkeypatch.setattr(cli, "ChromeActivityLease", _RecordingLease)
    monkeypatch.setattr(cli, "ensure_shared_chrome_running", fail_ensure)
    monkeypatch.setattr(cli, "new_run_dir", lambda _prefix: tmp_path)

    with pytest.raises(RuntimeError, match="lease assertion"):
        await command()
    assert events == ["enter", "exit"]


@pytest.mark.asyncio
async def test_worker_acquires_activity_before_parallel_slot(tmp_path, monkeypatch):
    events = []

    class _RecordingLease:
        def __enter__(self):
            events.append("activity_enter")
            return self

        def __exit__(self, *_):
            events.append("activity_exit")

    class _RecordingSlot:
        slot_id = 2

        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            assert events == ["activity_enter"]
            events.append("slot_enter")
            return self

        def __exit__(self, *_):
            events.append("slot_exit")

    async def browser_stub(*_a, **_k):
        assert events == ["activity_enter", "slot_enter"]
        return {"status": "ok"}

    monkeypatch.setattr(cli, "ChromeActivityLease", _RecordingLease)
    monkeypatch.setattr(cli, "ParallelSlot", _RecordingSlot)
    monkeypatch.setattr(cli, "_run_with_browser", browser_stub)

    result = await cli._browser_run("run", tmp_path, "prompt")

    assert result == {"status": "ok"}
    assert events == ["activity_enter", "slot_enter", "slot_exit", "activity_exit"]
