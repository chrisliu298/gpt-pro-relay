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
