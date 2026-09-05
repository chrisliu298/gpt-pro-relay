"""Regression tests for the cmd_ask prompt-size guard (MAX_PROMPT_BYTES).

Commit d6d577d raised MAX_PROMPT_BYTES from 1MB to 5MB. The guard in `cmd_ask`
rejects `len(prompt.encode()) > MAX_PROMPT_BYTES` with exit 2 /
`prompt_too_large` *before* creating a run dir or spawning a worker; a prompt of
exactly MAX_PROMPT_BYTES is accepted. These pin that boundary (relative to the
constant, not a hardcoded value) so a future edit to the constant, to stdin
handling, or to the guard ordering can't silently move it — e.g. reject an
at-cap prompt, or accept an over-cap one after already writing a run dir.
"""

import io
import types

import pytest

from gpt_pro import cli


def _args(run_id="test-cap"):
    # no_wait=True short-circuits cmd_ask before _wait_for_result, so the
    # accept path returns without needing a live worker.
    return types.SimpleNamespace(
        run_id=run_id, no_wait=True, generation_timeout=1.0, output=None
    )


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Redirect RUNS + CLAIMS to a temp dir; capture stderr_jsonl + _spawn_worker.

    CLAIMS matters even though nothing here asserts on it: cmd_ask takes a
    RunClaim, so leaving it unpatched writes a real lock file into the
    developer's ~/.gpt-pro/claims/.
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(cli, "RUNS", runs)
    monkeypatch.setattr(cli, "CLAIMS", tmp_path / "claims")
    monkeypatch.setattr(cli, "ACCOUNT_ROUTER_LOCK", tmp_path / "account-router.lock")
    monkeypatch.setattr(cli, "ACCOUNT_ROUTER_STATE", tmp_path / "account-router.json")
    emitted = []
    monkeypatch.setattr(cli, "stderr_jsonl", lambda d: emitted.append(d))
    spawned = []
    monkeypatch.setattr(cli, "_spawn_worker", lambda rid, rd: spawned.append((rid, rd)))
    return types.SimpleNamespace(runs=runs, emitted=emitted, spawned=spawned)


def _set_stdin(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


async def test_at_cap_accepted(harness, monkeypatch):
    # Exactly MAX_PROMPT_BYTES bytes (ASCII -> 1 byte/char) must pass the guard.
    _set_stdin(monkeypatch, "a" * cli.MAX_PROMPT_BYTES)
    rc = await cli.cmd_ask(_args())
    assert rc == 0
    assert len(harness.spawned) == 1  # worker spawned
    run_dir = harness.runs / "test-cap"
    assert (run_dir / "prompt.md").exists()
    assert (run_dir / "meta.json").exists()
    assert harness.emitted[-1]["status"] == "submitted"


async def test_over_cap_rejected_no_run_created(harness, monkeypatch):
    # One byte over the cap must fail closed with no side effects on disk.
    _set_stdin(monkeypatch, "a" * (cli.MAX_PROMPT_BYTES + 1))
    rc = await cli.cmd_ask(_args())
    assert rc == 2
    assert harness.emitted[-1]["reason"] == "prompt_too_large"
    assert harness.emitted[-1]["limit"] == cli.MAX_PROMPT_BYTES
    assert harness.spawned == []  # no worker
    assert not (harness.runs / "test-cap").exists()  # no run dir
    assert list(harness.runs.iterdir()) == []  # nothing written at all


async def test_multibyte_counted_as_bytes_not_chars(harness, monkeypatch):
    # The guard measures UTF-8 bytes, not characters: a string of
    # (cap // 2 + 1) 2-byte chars exceeds the cap even though its char count
    # is ~half. Pins that the check stays byte-based.
    two_byte_char = "é"  # 'é' -> 2 bytes in UTF-8
    n_chars = cli.MAX_PROMPT_BYTES // 2 + 1
    _set_stdin(monkeypatch, two_byte_char * n_chars)
    rc = await cli.cmd_ask(_args())
    assert rc == 2
    assert harness.emitted[-1]["reason"] == "prompt_too_large"
    assert harness.spawned == []


async def test_empty_prompt_rejected(harness, monkeypatch):
    # Whitespace-only stdin is rejected earlier, before the size guard.
    _set_stdin(monkeypatch, "   \n\t ")
    rc = await cli.cmd_ask(_args())
    assert rc == 2
    assert harness.emitted[-1]["reason"] == "empty_prompt"
    assert harness.spawned == []
