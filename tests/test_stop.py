"""Graceful stop: `stop <run-id>` interrupts a run — dequeue if not yet sent,
else click ChatGPT's Stop button on the live turn.

The mechanism is a file signal (`stop.request` in run_dir) the owning worker
polls at its phase gates; the command itself never drives the browser (worker-
only v1). These pin: the signal helpers, the queued-dequeue (ParallelSlot raises
RunStopped), the Stop-button click helper, the terminal reporting, and the
command's branches — unknown run, already finished, no live worker (a
non-contending process check, NOT a RunClaim probe), and a worker that
finalizes the stop.
"""

import json
import types

import pytest

from gpt_pro import cli
from gpt_pro.cli import cmd_stop, stop_requested


@pytest.fixture
def _isolate(monkeypatch, tmp_path):
    """Never touch the real ~/.gpt-pro."""
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(cli, "RUNS", runs)
    monkeypatch.setattr(cli, "CLAIMS", tmp_path / "claims")
    monkeypatch.setattr(cli, "SLOT_LOCK_DIR", tmp_path / "slots")
    return types.SimpleNamespace(runs=runs)


def _mk_run(runs, run_id="r1", *, result=None):
    rd = runs / run_id
    rd.mkdir()
    (rd / "meta.json").write_text("{}")
    if result is not None:
        (rd / "result.json").write_text(json.dumps(result))
    return rd


# ---- signal + result helpers ----

def test_stop_requested_reflects_the_file(tmp_path):
    assert stop_requested(tmp_path) is False
    (tmp_path / cli.STOP_REQUEST).write_text("{}")
    assert stop_requested(tmp_path) is True


async def test_write_stop_signal_is_idempotent_and_race_safe(tmp_path):
    cli.write_stop_signal(tmp_path)
    assert (tmp_path / cli.STOP_REQUEST).exists()
    # A second (concurrent) producer must not raise — existence is the signal.
    cli.write_stop_signal(tmp_path)
    assert (tmp_path / cli.STOP_REQUEST).exists()


def test_worker_process_alive_reads_pgrep_returncode(monkeypatch):
    class _R:
        def __init__(self, rc):
            self.returncode = rc

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _R(0))
    assert cli._worker_process_alive("r1") is True
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _R(1))
    assert cli._worker_process_alive("r1") is False


def test_worker_process_alive_pgrep_error_codes_fail_safe_to_alive(monkeypatch):
    # Only pgrep exit 1 ("no match") proves a dead worker. 2/3 are operational
    # errors (bad pattern / internal) and MUST fail-safe to alive — never a
    # spurious no_live_worker while a worker is still running.
    class _R:
        def __init__(self, rc):
            self.returncode = rc

    for rc in (2, 3, 4):
        monkeypatch.setattr(cli.subprocess, "run", lambda *a, _rc=rc, **k: _R(_rc))
        assert cli._worker_process_alive("r1") is True, f"rc={rc}"


def test_worker_process_alive_falls_back_to_alive_on_error(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("no pgrep")

    monkeypatch.setattr(cli.subprocess, "run", _boom)
    # On any probe error, assume alive — never falsely report a live worker dead.
    assert cli._worker_process_alive("r1") is True


def test_worker_process_alive_pattern_is_escaped_and_anchored(monkeypatch):
    # The pgrep pattern must escape regex specials in the run-id (so `.` is not a
    # wildcard) and anchor a boundary (so target `r1` never matches worker `r10`).
    import re as _re

    captured = {}

    class _R:
        returncode = 1

    def _capture(args, **_k):
        captured["args"] = args
        return _R()

    monkeypatch.setattr(cli.subprocess, "run", _capture)
    rid = "ask-2026.01-r1"
    cli._worker_process_alive(rid)
    pattern = captured["args"][2]  # ["pgrep", "-f", <pattern>]
    assert pattern == r"gpt_pro\.cli _run " + _re.escape(rid) + r"([[:space:]]|$)"
    assert _re.escape(rid) in pattern  # the id's `.` / `-` are escaped, not wildcards


def test_stopped_result_shape(tmp_path):
    r = cli._stopped_result("r1", tmp_path, "stopped_after_send")
    assert r["status"] == "stopped"
    assert r["reason"] == "stopped_after_send"
    assert r["exit_code"] == 5
    assert r["run_id"] == "r1"


def test_emit_terminal_stopped_returns_5_and_prints_nothing(tmp_path, capsys):
    rc = cli._emit_terminal(
        {"status": "stopped", "reason": "stopped_after_send", "run_id": "r1"}, tmp_path
    )
    assert rc == 5
    # Discard policy: no response body is streamed to stdout.
    assert capsys.readouterr().out == ""


# ---- queued dequeue (ParallelSlot) ----

def test_parallelslot_dequeues_when_stopped(_isolate):
    # A stop seen while queued raises RunStopped BEFORE any slot is acquired.
    with pytest.raises(cli.RunStopped):
        cli.ParallelSlot(6, stop_check=lambda: True).__enter__()


def test_parallelslot_acquires_when_not_stopped(_isolate):
    slot = cli.ParallelSlot(2, stop_check=lambda: False)
    got = slot.__enter__()
    try:
        assert got.slot_id == 0  # not dequeued → normal acquisition
    finally:
        slot.__exit__()


# ---- _click_stop_button ----

class _FakeLoc:
    def __init__(self, count, *, click_raises=None):
        self._count = count
        self.clicked = False
        self._click_raises = click_raises

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def click(self, timeout=None):
        if self._click_raises is not None:
            raise self._click_raises
        self.clicked = True


class _ClickPage:
    def __init__(self, count, *, closed=False, click_raises=None):
        self.loc = _FakeLoc(count, click_raises=click_raises)
        self._closed = closed

    def locator(self, _sel):
        return self.loc

    def is_closed(self):
        return self._closed


async def test_click_stop_button_absent_returns_false():
    page = _ClickPage(0)
    assert await cli._click_stop_button(page) is False


async def test_click_stop_button_present_clicks_and_returns_true():
    page = _ClickPage(1)
    assert await cli._click_stop_button(page) is True
    assert page.loc.clicked is True


async def test_click_stop_button_close_raises_run_page_closed():
    # A close during the click raises RunPageClosed (routes to the recovery
    # loop) rather than being laundered into a False.
    page = _ClickPage(1, closed=True, click_raises=RuntimeError("target closed"))
    with pytest.raises(cli.RunPageClosed):
        await cli._click_stop_button(page)


async def test_click_stop_button_live_failure_returns_false():
    # A live-page click error (tab still open) is non-fatal — the turn may have
    # just finished; bias toward "nothing to stop".
    page = _ClickPage(1, closed=False, click_raises=RuntimeError("transient"))
    assert await cli._click_stop_button(page) is False


# ---- cmd_stop branches ----

async def test_cmd_stop_unknown_run_returns_4(_isolate):
    rc = await cmd_stop(types.SimpleNamespace(run_id="nope", timeout=1.0))
    assert rc == 4


async def test_cmd_stop_already_finished_returns_0_and_clears_stale_signal(_isolate):
    rd = _mk_run(_isolate.runs, result={"status": "ok"})
    (rd / cli.STOP_REQUEST).write_text("{}")  # a stale signal from a prior stop
    rc = await cmd_stop(types.SimpleNamespace(run_id="r1", timeout=1.0))
    assert rc == 0
    # An already-finished run is never (re)signalled, and any stale signal is cleared.
    assert not (rd / cli.STOP_REQUEST).exists()


async def _no_result(_run_dir, *, timeout=None, poll_interval=0.5):
    return None


async def test_cmd_stop_no_live_worker_returns_2(_isolate, monkeypatch):
    # No terminal result within the window AND no worker process alive → the
    # dead-worker report. The signal stays written (a late worker could still
    # consume it). Liveness is a non-contending process check, NOT a claim probe.
    rd = _mk_run(_isolate.runs)
    monkeypatch.setattr(cli, "_wait_for_result", _no_result)
    monkeypatch.setattr(cli, "_worker_process_alive", lambda _run_id: False)
    rc = await cmd_stop(types.SimpleNamespace(run_id="r1", timeout=0.01))
    assert rc == 2
    assert (rd / cli.STOP_REQUEST).exists()


async def test_cmd_stop_terminal_reports_stopped_and_cleans_signal(_isolate, monkeypatch):
    rd = _mk_run(_isolate.runs)

    async def _fake_wait(_run_dir, *, timeout=None, poll_interval=0.5):
        return {"status": "stopped", "reason": "stopped_after_send", "run_id": "r1"}

    monkeypatch.setattr(cli, "_wait_for_result", _fake_wait)
    rc = await cmd_stop(types.SimpleNamespace(run_id="r1", timeout=1.0))
    assert rc == 0
    # The now-inert signal is cleaned up once a terminal result is observed.
    assert not (rd / cli.STOP_REQUEST).exists()


async def test_cmd_stop_live_worker_pending_returns_0(_isolate, monkeypatch):
    # Worker process alive but hasn't finalized within the window → pending (0).
    _mk_run(_isolate.runs)
    monkeypatch.setattr(cli, "_wait_for_result", _no_result)
    monkeypatch.setattr(cli, "_worker_process_alive", lambda _run_id: True)
    rc = await cmd_stop(types.SimpleNamespace(run_id="r1", timeout=0.01))
    assert rc == 0


# ---- _run_claimed refuses to rerun a terminal run (stale-signal clobber guard) ----

async def test_run_claimed_refuses_terminal_rerun(_isolate, monkeypatch):
    rd = _mk_run(_isolate.runs, result={"status": "ok", "exit_code": 0})
    (rd / "prompt.md").write_text("hi")
    # A stale stop.request must NOT let a rerun clobber the `ok` result → `stopped`.
    (rd / cli.STOP_REQUEST).write_text("{}")

    async def _explode(*_a, **_k):
        raise AssertionError("_browser_run must NOT run on a terminal run_dir")

    monkeypatch.setattr(cli, "_browser_run", _explode)
    rc = await cli._run_claimed("r1", rd)
    assert rc == 0  # returns the recorded exit_code and touches nothing
    assert json.loads((rd / "result.json").read_text())["status"] == "ok"


async def test_run_claimed_terminal_guard_precedes_missing_prompt(_isolate):
    # The terminal guard must run BEFORE missing-prompt handling: a terminal
    # result with NO prompt.md must stay untouched, not be clobbered to
    # `missing_prompt`/error.
    rd = _isolate.runs / "r1"
    rd.mkdir()
    (rd / "meta.json").write_text("{}")
    (rd / "result.json").write_text(json.dumps({"status": "ok", "exit_code": 0}))
    # deliberately no prompt.md
    rc = await cli._run_claimed("r1", rd)
    assert rc == 0
    assert json.loads((rd / "result.json").read_text())["status"] == "ok"
