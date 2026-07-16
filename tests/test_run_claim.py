"""Tests for the per-run claim — the single-writer guarantee on a run_dir.

`ask` decides a run's existence (does run_dir exist? same prompt_sha? spawn or
attach?) and `_run` owns its artifacts. Without a claim, two concurrent
same-run-id submits can both observe absence and spawn a worker each; the two
workers then share `response.pending.md`, and one can publish the other's body
to `response.md` — an answer its own audit never saw. (A GPT review forced that
schedule against 5223fb7: both calls exited 0, spawn_count was 2, and the run
returned `ok` carrying the *other* worker's wrong-model body.) It predates the
artifact lifecycle — the workers previously raced on `response.md` directly —
but the lifecycle's single-writer premise is what makes it load-bearing.

One lock file, two roles: `ask` holds it briefly while deciding, `_run` holds it
for the whole run. The release-before-spawn ordering is the subtle part and is
pinned below.
"""

import fcntl
import io
import json
import types

import pytest

from gpt_pro import cli


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Never touch the real ~/.gpt-pro."""
    runs = tmp_path / "runs"
    runs.mkdir()
    claims = tmp_path / "claims"
    monkeypatch.setattr(cli, "RUNS", runs)
    monkeypatch.setattr(cli, "CLAIMS", claims)
    return types.SimpleNamespace(runs=runs, claims=claims)


# ---- the primitive ----

def test_claim_is_exclusive_across_fds():
    # flock conflicts across two *file descriptors* even inside one process —
    # the same property the ParallelSlot skip_slot_id bug turned on. A claim
    # that only excluded across processes would not protect a threaded caller.
    a = cli.RunClaim("run-1")
    b = cli.RunClaim("run-1", blocking=False)
    assert a.acquire() is True
    try:
        assert b.acquire() is False, "second claim on the same run must fail"
    finally:
        a.release()


def test_claim_is_per_run_not_global():
    # Two different runs must not serialize against each other — that would
    # silently reintroduce the whole-section serialization the slot semaphore
    # exists to avoid.
    a = cli.RunClaim("run-1")
    b = cli.RunClaim("run-2", blocking=False)
    assert a.acquire() is True
    try:
        assert b.acquire() is True
        b.release()
    finally:
        a.release()


def test_claim_reacquirable_after_release():
    a = cli.RunClaim("run-1")
    assert a.acquire() is True
    a.release()
    b = cli.RunClaim("run-1", blocking=False)
    assert b.acquire() is True
    b.release()


def test_blocking_claim_raises_rather_than_returning_false():
    # The context-manager form is for the blocking role; a False return there
    # would be silently ignored by `with`.
    a = cli.RunClaim("run-1")
    a.acquire()
    try:
        with pytest.raises(BlockingIOError):
            with cli.RunClaim("run-1", blocking=False):
                pass
    finally:
        a.release()


# ---- ask: decide under the claim, spawn outside it ----

def _args(run_id="claim-test"):
    return types.SimpleNamespace(
        run_id=run_id, no_wait=True, generation_timeout=1.0, output=None
    )


async def test_ask_claims_before_deciding_and_releases_before_spawn(monkeypatch):
    # Ordering is load-bearing in BOTH directions:
    #  - claim before the exists/sha check, or two submits both conclude "new";
    #  - release before the spawn, or the worker's own non-blocking acquire
    #    fails against its parent and every run dies at birth.
    events = []
    real_claim = cli.RunClaim

    class _SpyClaim(real_claim):
        def acquire(self):
            events.append("claim")
            return super().acquire()

        def release(self):
            events.append("release")
            return super().release()

    monkeypatch.setattr(cli, "RunClaim", _SpyClaim)
    monkeypatch.setattr(cli, "stderr_jsonl", lambda d: None)

    def _spy_spawn(run_id, run_dir):
        events.append("spawn")
        # The decisive probe: if ask still held the claim here, a real worker
        # would fail its acquire and exit immediately.
        probe = real_claim(run_id, blocking=False)
        events.append("claim_free_at_spawn" if probe.acquire() else "CLAIM_STILL_HELD")
        probe.release()

    monkeypatch.setattr(cli, "_spawn_worker", _spy_spawn)
    monkeypatch.setattr("sys.stdin", io.StringIO("hello"))

    rc = await cli.cmd_ask(_args())
    assert rc == 0
    assert events.index("claim") < events.index("release") < events.index("spawn")
    assert "claim_free_at_spawn" in events
    assert "CLAIM_STILL_HELD" not in events


async def test_ask_attaches_without_spawning_when_run_exists(monkeypatch, _isolate_state):
    # The claim must not disturb the idempotent-attach path: same run-id, same
    # prompt -> no second worker.
    spawned = []
    monkeypatch.setattr(cli, "_spawn_worker", lambda rid, rd: spawned.append(rid))
    emitted = []
    monkeypatch.setattr(cli, "stderr_jsonl", lambda d: emitted.append(d))

    monkeypatch.setattr("sys.stdin", io.StringIO("hello"))
    assert await cli.cmd_ask(_args()) == 0
    assert len(spawned) == 1

    monkeypatch.setattr("sys.stdin", io.StringIO("hello"))
    assert await cli.cmd_ask(_args()) == 0
    assert len(spawned) == 1, "attach must not spawn a second worker"
    assert emitted[-1].get("attached") is True


# ---- worker: refuse a run someone else owns ----

async def test_worker_refuses_and_touches_nothing_when_already_claimed(
    monkeypatch, _isolate_state
):
    # A second worker on a live run (a manual `_run`, a future spawn path) must
    # exit without writing ANY artifact. Writing result.json would race the
    # owner's verdict; staging would corrupt the owner's body.
    run_dir = _isolate_state.runs / "claim-test"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("hello")

    owner = cli.RunClaim("claim-test")
    assert owner.acquire() is True

    emitted = []
    monkeypatch.setattr(cli, "stderr_jsonl", lambda d: emitted.append(d))
    ran = []
    monkeypatch.setattr(cli, "_browser_run", lambda *a, **k: ran.append(a))

    try:
        rc = await cli.cmd_run(types.SimpleNamespace(run_id="claim-test"))
    finally:
        owner.release()

    assert rc == 1
    assert not ran, "the losing worker must never reach the browser"
    assert not (run_dir / "result.json").exists(), "must not race the owner's verdict"
    assert not (run_dir / cli.RESPONSE_STAGED).exists()
    assert emitted[-1]["reason"] == "run_already_claimed"


async def test_worker_runs_and_releases_when_unclaimed(monkeypatch, _isolate_state):
    # The happy path still works, and the claim is released afterwards so a
    # later reattach/retry isn't locked out by a finished run.
    run_dir = _isolate_state.runs / "claim-test"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("hello")

    async def _fake_browser_run(run_id, rd, prompt_text):
        # The claim must be HELD for the duration of the actual run.
        probe = cli.RunClaim(run_id, blocking=False)
        assert probe.acquire() is False, "worker must hold its claim while running"
        return {"status": "ok", "exit_code": 0}

    monkeypatch.setattr(cli, "_browser_run", _fake_browser_run)
    monkeypatch.setattr(cli, "stderr_jsonl", lambda d: None)

    rc = await cli.cmd_run(types.SimpleNamespace(run_id="claim-test"))
    assert rc == 0
    assert json.loads((run_dir / "result.json").read_text())["status"] == "ok"

    after = cli.RunClaim("claim-test", blocking=False)
    assert after.acquire() is True, "claim must be released when the run ends"
    after.release()
